#!/usr/bin/env python3
r"""
app.py — live welcome-screen web server (Render-deployable)

Runs a small web server that always shows the next arriving guest at
Villa Brushy Creek. A background thread re-fetches OwnerRez every hour
and updates the page in memory, so anyone loading the URL (e.g. the
tablet at the front desk) always sees current info without you having
to regenerate or re-copy any files.

DEPLOYING TO RENDER
--------------------
1. Push this folder to a GitHub repo (see README.md in this folder
   for the exact git commands).
2. In the Render dashboard: New -> Web Service -> connect that repo.
3. Runtime: Python 3. Render auto-detects requirements.txt.
   Build command:  pip install -r requirements.txt
   Start command:  gunicorn app:app
4. Instance type: choose "Starter" ($7/mo, always-on, no cold starts)
   or "Free" (sleeps after 15 min idle) under the instance type picker
   on the same New Web Service screen.
5. Add environment variables (Environment tab):
     OWNERREZ_USERNAME = you@example.com
     OWNERREZ_TOKEN     = your-api-token
6. Deploy. Render gives you a stable URL like
   https://villa-brushy-creek-welcome.onrender.com
   -> point the tablet's browser at that URL.

Render sets the PORT environment variable itself and routes external
traffic to it — this file reads PORT dynamically (see bottom) instead
of hardcoding 8080, which is required for Render (and most PaaS hosts)
to work.

RUNNING LOCALLY (optional, for testing before you deploy)
------------------------------------------------------------
   pip install -r requirements.txt
   set OWNERREZ_USERNAME=you@example.com      (Windows)
   set OWNERREZ_TOKEN=your-api-token
   python app.py
   -> open http://localhost:8080/
"""

import os
import sys
import io
import base64
import threading
import time
import datetime
import requests
from flask import Flask, Response, request, redirect

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
OWNERREZ_USERNAME = os.environ.get("OWNERREZ_USERNAME")
OWNERREZ_TOKEN = os.environ.get("OWNERREZ_TOKEN")
API_BASE = "https://api.ownerrez.com/v2"

REFRESH_SECONDS = 60 * 60  # 1 hour
UPCOMING_COUNT = 5  # how many future bookings to track for the /manage page
# Render (and most PaaS hosts) assign the port dynamically via $PORT.
# Falls back to 8080 for local testing where PORT isn't set.
PORT = int(os.environ.get("PORT", 8080))

PROPERTY_DISPLAY_NAME = "Villa Brushy Creek"  # fallback label if API omits it

# Wi-Fi auto-join QR code. All optional -- if WIFI_SSID isn't set, the
# Wi-Fi section is simply omitted from the page. WIFI_AUTH is "WPA"
# (covers WPA/WPA2/WPA3 -- what almost every home router uses), "WEP",
# or "nopass" for an open network.
WIFI_SSID = os.environ.get("WIFI_SSID")
WIFI_PASSWORD = os.environ.get("WIFI_PASSWORD", "")
WIFI_AUTH = os.environ.get("WIFI_AUTH", "WPA")

# Cleaning checklist shown on /cleaning for each upcoming booking.
# Override by setting CLEANING_TASKS to a comma-separated list, e.g.:
#   CLEANING_TASKS="Strip beds,Clean bathrooms,Vacuum floors"
_DEFAULT_CLEANING_TASKS = [
    "Strip and start all bedding laundry",
    "Remake all beds with fresh linens",
    "Clean and restock all bathrooms",
    "Clean kitchen — counters, appliances, dishes",
    "Vacuum and mop all floors",
    "Empty all trash and recycling",
    "Restock paper towels, toilet paper, soap",
    "Check pool/spa chemicals and skim debris",
    "Walk exterior and patio area",
    "Final walkthrough and lock up",
]
if os.environ.get("CLEANING_TASKS"):
    CLEANING_TASKS = [t.strip() for t in os.environ["CLEANING_TASKS"].split(",") if t.strip()]
else:
    CLEANING_TASKS = _DEFAULT_CLEANING_TASKS

# In-memory cache of everything the app needs to serve "/", "/manage",
# and "/cleaning". RLock (not Lock) because route handlers call
# _recompute_selected_and_render() while already holding the lock.
_cache_lock = threading.RLock()
_cache = {
    "upcoming": [],       # list of guest dicts, soonest arrival first
    "mode": "auto",       # "auto" -> always show the soonest arrival
                           # "manual" -> host has pinned a specific guest
    "selected_key": None, # booking_key of the guest currently being shown
    "html": "<h1>Loading first data from OwnerRez…</h1>",
    "last_updated": None,
    "last_error": None,
    # Cleaning checklist state: booking_key -> {task_name: bool}.
    # Keyed by task name (not index) so editing CLEANING_TASKS later
    # doesn't misalign already-saved progress for other tasks.
    "cleaning": {},
}


# ---------------------------------------------------------------------------
# 1. FETCH UPCOMING ARRIVALS FROM OWNERREZ
# ---------------------------------------------------------------------------
def _booking_key(b):
    """
    A stable identifier for a booking, used to remember which guest the
    host manually selected across hourly refreshes. Prefers OwnerRez's
    own booking id; falls back to the platform confirmation number
    (guaranteed present on every real booking) if id is ever missing.
    """
    return str(b.get("id") or b.get("platform_reservation_number")
               or f"{b.get('arrival')}::{b.get('guest', {}).get('first_name', 'guest')}")


def _booking_to_guest_dict(b):
    guest_first_name = b.get("guest", {}).get("first_name", "Guest")
    property_name = b.get("property", {}).get("name", PROPERTY_DISPLAY_NAME)
    return {
        "booking_key": _booking_key(b),
        "first_name": guest_first_name,
        "property_name": property_name,
        "arrival": datetime.date.fromisoformat(b["arrival"]),
        "departure": datetime.date.fromisoformat(b["departure"]),
        "check_in_time": b.get("check_in", "16:00"),
        "check_out_time": b.get("check_out", "11:00"),
        "adults": b.get("adults", 0),
        "children": b.get("children", 0),
        "platform": b.get("listing_site", "Direct"),
        "confirmation": b.get("platform_reservation_number", "—"),
    }


def fetch_upcoming_arrivals(limit=UPCOMING_COUNT):
    """
    Returns a list of up to `limit` guest dicts for the soonest upcoming
    active bookings, soonest arrival first.
    """
    if not OWNERREZ_USERNAME or not OWNERREZ_TOKEN:
        raise RuntimeError(
            "Missing credentials. Set OWNERREZ_USERNAME and OWNERREZ_TOKEN "
            "environment variables before running this script."
        )

    today = datetime.date.today()
    headers = {
        "User-Agent": "Villa Brushy Creek Welcome Screen/1.0",
        "Accept": "application/json",
    }

    # OwnerRez's v2 /bookings endpoint only supports since_utc, which
    # filters by *last-changed* date, not arrival/stay date -- there is
    # no server-side arrival filter on this endpoint. So we ask for
    # everything changed since a distant date (effectively "all
    # bookings"), paginate through results, and filter for future
    # arrivals ourselves.
    all_bookings = []
    offset = 0
    page_size = 100
    while True:
        params = {
            "since_utc": "2000-01-01T00:00:00Z",
            "include_guest": "true",
            "limit": page_size,
            "offset": offset,
        }
        resp = requests.get(
            f"{API_BASE}/bookings",
            params=params,
            auth=(OWNERREZ_USERNAME, OWNERREZ_TOKEN),
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        # Different OwnerRez endpoints/versions key the list differently --
        # handle both to be safe.
        page = payload.get("items") or payload.get("bookings") or []
        all_bookings.extend(page)
        if len(page) < page_size:
            break  # last page
        offset += page_size
        if offset > 2000:
            break  # safety cap against runaway pagination

    upcoming = [
        b for b in all_bookings
        if b.get("status") == "active"
        and "arrival" in b
        and datetime.date.fromisoformat(b["arrival"]) >= today
    ]
    if not upcoming:
        raise RuntimeError(
            f"No upcoming active bookings found (checked {len(all_bookings)} "
            f"total bookings from the API)."
        )

    upcoming.sort(key=lambda b: b["arrival"])
    return [_booking_to_guest_dict(b) for b in upcoming[:limit]]


# ---------------------------------------------------------------------------
# 2. RENDER THE HTML
# ---------------------------------------------------------------------------
def format_date(d):
    return d.strftime("%a, %b ") + str(d.day)


def format_time_12h(t):
    try:
        h, m = t.split(":")
        h = int(h)
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12
        if h12 == 0:
            h12 = 12
        return f"{h12}:{m} {suffix}"
    except Exception:
        return t


def _wifi_qr_escape(value):
    # Per the Wi-Fi QR spec, these characters must be backslash-escaped
    # inside each field: backslash, semicolon, comma, colon.
    for ch in ("\\", ";", ",", ":"):
        value = value.replace(ch, "\\" + ch)
    return value


def generate_wifi_qr_data_uri():
    """
    Builds a QR code that phones can scan to auto-join the guest Wi-Fi
    (no typing the password). Returns a base64 data: URI for a PNG, or
    None if WIFI_SSID isn't configured. Computed once at startup and
    cached in memory -- the network doesn't change hour to hour, so
    there's no need to regenerate this on every refresh.
    """
    if not WIFI_SSID:
        return None

    import qrcode  # imported lazily so the app still runs without this
    # optional dependency installed if Wi-Fi isn't configured

    auth = WIFI_AUTH if WIFI_AUTH in ("WPA", "WEP", "nopass") else "WPA"
    payload = "WIFI:T:{auth};S:{ssid};P:{password};;".format(
        auth=auth,
        ssid=_wifi_qr_escape(WIFI_SSID),
        password=_wifi_qr_escape(WIFI_PASSWORD) if auth != "nopass" else "",
    )

    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#142B29", back_color="#EFEAD9")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


# Generated once at import time (not per-request/per-refresh) since the
# Wi-Fi network doesn't change hour to hour.
try:
    _WIFI_QR_DATA_URI = generate_wifi_qr_data_uri()
except Exception as e:
    print(f"Wi-Fi QR generation failed, omitting Wi-Fi section: {e}", file=sys.stderr)
    _WIFI_QR_DATA_URI = None


def render_html(g):
    nights = (g["departure"] - g["arrival"]).days
    party_bits = [f"{g['adults']} adult{'s' if g['adults'] != 1 else ''}"]
    if g["children"]:
        party_bits.append(f"{g['children']} child{'ren' if g['children'] != 1 else ''}")
    party_str = " · ".join(party_bits)

    today = datetime.date.today()
    days_out = (g["arrival"] - today).days
    if days_out <= 0:
        countdown_str = "Arriving today"
    elif days_out == 1:
        countdown_str = "Arriving tomorrow"
    else:
        countdown_str = f"<b>{days_out}</b>&nbsp;days until {g['first_name']}'s group arrives"

    if _WIFI_QR_DATA_URI:
        wifi_section = WIFI_SECTION_TEMPLATE.format(
            qr_data_uri=_WIFI_QR_DATA_URI,
            ssid=WIFI_SSID,
        )
    else:
        wifi_section = ""

    return TEMPLATE.format(
        first_name=g["first_name"],
        property_name=g["property_name"],
        property_name_upper=g["property_name"].upper(),
        arrival_str=format_date(g["arrival"]),
        departure_str=format_date(g["departure"]),
        check_in_time=format_time_12h(g["check_in_time"]),
        check_out_time=format_time_12h(g["check_out_time"]),
        nights=nights,
        nights_label="night" if nights == 1 else "nights",
        party_str=party_str,
        platform=g["platform"],
        confirmation=g["confirmation"],
        countdown_str=countdown_str,
        wifi_section=wifi_section,
    )


WIFI_SECTION_TEMPLATE = """
  <h2 class="section-title">Connect to Wi-Fi</h2>
  <div class="wifi-card">
    <img class="wifi-qr" src="{qr_data_uri}" alt="Wi-Fi QR code" width="160" height="160">
    <div class="wifi-info">
      <div class="wifi-label">Scan to join automatically</div>
      <div class="wifi-network">{ssid}</div>
      <div class="wifi-hint">Or connect manually in your phone's Wi-Fi settings.</div>
    </div>
  </div>
"""


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Welcome to {property_name}</title>
<meta http-equiv="refresh" content="3600">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,500;9..144,600&family=Work+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{{
    --creek: #1F3F3D;
    --creek-deep: #142B29;
    --limestone: #EFEAD9;
    --sage: #7C8B65;
    --clay: #C1652F;
    --bark: #2A2018;
  }}
  *{{box-sizing:border-box;}}
  body{{
    margin:0;
    background: var(--limestone);
    color: var(--bark);
    font-family:'Work Sans', sans-serif;
    -webkit-font-smoothing:antialiased;
  }}
  .wrap{{ max-width: 1180px; margin:0 auto; padding: 0 40px 50px; }}
  .hero{{
    background: linear-gradient(180deg, var(--creek) 0%, var(--creek-deep) 100%);
    color: var(--limestone);
    padding: 56px 28px 40px;
    border-radius: 0 0 28px 28px;
    position: relative;
    overflow: hidden;
  }}
  .hero-top{{
    display:flex;
    justify-content:space-between;
    align-items:flex-start;
    gap: 24px;
    flex-wrap: wrap;
  }}
  .hero-left{{ flex: 1 1 260px; min-width: 200px; }}
  .hero-right{{
    flex: 0 1 380px;
    min-width: 320px;
    background: rgba(239,234,217,0.08);
    border: 1px solid rgba(239,234,217,0.2);
    border-radius: 18px;
    padding: 30px 34px;
  }}
  .res-row{{ display:flex; gap:28px; }}
  .res-divider{{ height:1px; background: rgba(239,234,217,0.15); margin: 20px 0; }}
  .res-stat{{ flex:1; }}
  .res-label{{
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: #B9CFC2;
    margin-bottom: 7px;
  }}
  .res-value{{
    font-family:'Fraunces', serif;
    font-weight: 500;
    font-size: 28px;
    color: var(--limestone);
  }}
  .res-value-sm{{ font-size: 22px; }}
  .res-sub{{ font-size: 15px; color:#B9CFC2; margin-top:4px; }}
  .eyebrow{{
    font-size: 13px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #B9CFC2;
    margin: 0 0 18px;
  }}
  h1{{
    font-family:'Fraunces', serif;
    font-weight: 500;
    font-size: clamp(34px, 4.5vw, 64px);
    line-height: 1.02;
    margin: 0 0 10px;
  }}
  .property{{
    font-family:'Fraunces', serif;
    font-style: italic;
    font-weight: 300;
    font-size: 21px;
    color: #D8CBA6;
    margin: 0 0 30px;
  }}
  .countdown{{
    display:inline-flex;
    align-items:baseline;
    gap:10px;
    background: rgba(239,234,217,0.09);
    border: 1px solid rgba(239,234,217,0.25);
    border-radius: 100px;
    padding: 8px 18px 8px 16px;
    font-size: 14px;
    color: #E8E2CE;
  }}
  .countdown b{{
    font-family:'Fraunces', serif;
    font-size: 20px;
    font-weight: 600;
    color: var(--limestone);
  }}
  .creek-divider{{ display:block; width:100%; height:56px; margin-top:24px; }}
  .creek-divider path{{
    fill:none;
    stroke: var(--sage);
    stroke-width: 2.5;
    stroke-linecap: round;
    stroke-dasharray: 900;
    stroke-dashoffset: 900;
    animation: draw 1.8s ease forwards 0.3s;
  }}
  @keyframes draw{{ to{{ stroke-dashoffset: 0; }} }}
  .lower-grid{{
    display:grid;
    grid-template-columns: 1.3fr 1fr;
    gap: 48px;
    align-items:start;
    margin-top: 44px;
  }}
  @media (max-width: 800px){{
    .lower-grid{{ grid-template-columns: 1fr; gap: 8px; }}
  }}
  .section-title{{
    font-family:'Fraunces', serif;
    font-weight:500;
    font-size: 22px;
    color: var(--creek-deep);
    margin: 0 0 16px;
  }}
  .path{{
    position: relative;
    padding-left: 30px;
    border-left: 2px solid #DCD4B8;
    margin-left: 6px;
  }}
  .step{{ position: relative; padding-bottom: 26px; }}
  .step:last-child{{ padding-bottom:0; }}
  .step::before{{
    content:'';
    position:absolute;
    left:-37px;
    top:2px;
    width:12px;
    height:12px;
    border-radius:50%;
    background: var(--clay);
    border: 3px solid var(--limestone);
    box-shadow: 0 0 0 1px #DCD4B8;
  }}
  .step .step-title{{
    font-weight:600;
    font-size: 15px;
    color: var(--bark);
    margin-bottom:3px;
  }}
  .step .step-detail{{ font-size: 14px; color:#5C5443; line-height:1.5;}}
  .wifi-card{{
    display:flex;
    align-items:center;
    gap: 20px;
    background: #fff;
    border: 1px solid #E2DBC5;
    border-radius: 14px;
    padding: 20px;
  }}
  .wifi-qr{{
    flex-shrink: 0;
    width: 100px;
    height: 100px;
    border-radius: 8px;
    border: 1px solid #E2DBC5;
  }}
  .wifi-label{{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: #8A7F63;
    margin-bottom: 4px;
  }}
  .wifi-network{{
    font-family:'Fraunces', serif;
    font-weight: 500;
    font-size: 20px;
    color: var(--creek-deep);
    margin-bottom: 6px;
  }}
  .wifi-hint{{ font-size: 13px; color:#77705C; }}
  .note{{
    margin-top: 24px;
    background: #F6F1E1;
    border: 1px solid #E5DCB9;
    border-radius: 14px;
    padding: 18px 20px;
    font-size: 14px;
    color: #5C5443;
    line-height: 1.6;
  }}
  .note b{{ color: var(--creek-deep); }}
  .footer{{
    margin-top: 34px;
    text-align:center;
    font-size: 12px;
    color:#9A9276;
    letter-spacing:0.04em;
  }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="hero-top">
      <div class="hero-left">
        <p class="eyebrow">Upcoming Arrival</p>
        <h1>Welcome,<br>{first_name}</h1>
        <p class="property">{property_name}</p>
        <div class="countdown">{countdown_str}</div>
      </div>
      <div class="hero-right">
        <div class="res-row">
          <div class="res-stat">
            <div class="res-label">Arrival</div>
            <div class="res-value">{arrival_str}</div>
            <div class="res-sub">From {check_in_time}</div>
          </div>
          <div class="res-stat">
            <div class="res-label">Departure</div>
            <div class="res-value">{departure_str}</div>
            <div class="res-sub">By {check_out_time}</div>
          </div>
        </div>
        <div class="res-divider"></div>
        <div class="res-row">
          <div class="res-stat">
            <div class="res-label">Stay</div>
            <div class="res-value res-value-sm">{nights} {nights_label}</div>
          </div>
          <div class="res-stat">
            <div class="res-label">Party</div>
            <div class="res-value res-value-sm">{party_str}</div>
          </div>
        </div>
        <div class="res-divider"></div>
        <div class="res-row">
          <div class="res-stat">
            <div class="res-label">Booked via</div>
            <div class="res-value res-value-sm">{platform}</div>
          </div>
          <div class="res-stat">
            <div class="res-label">Confirmation</div>
            <div class="res-value res-value-sm">{confirmation}</div>
          </div>
        </div>
      </div>
    </div>
    <svg class="creek-divider" viewBox="0 0 700 56" preserveAspectRatio="none">
      <path d="M0,28 C70,10 140,46 210,28 C280,10 350,46 420,28 C490,10 560,46 630,28 C660,20 680,30 700,26" />
    </svg>
  </div>

  <div class="lower-grid">
    <div class="col-main">
      <h2 class="section-title">The path in</h2>
      <div class="path">
        <div class="step">
          <div class="step-title">Booking confirmed</div>
          <div class="step-detail">Reserved via {platform} — everything's locked in on our end.</div>
        </div>
        <div class="step">
          <div class="step-title">Arrival — {arrival_str}</div>
          <div class="step-detail">Check-in opens at {check_in_time}. We'll have the villa ready and waiting.</div>
        </div>
        <div class="step">
          <div class="step-title">Departure — {departure_str}</div>
          <div class="step-detail">Check-out by {check_out_time}. We hope the creek treats you well.</div>
        </div>
      </div>
    </div>

    <div class="col-side">
{wifi_section}
      <div class="note">
        <b>Host note —</b> this screen refreshes automatically from OwnerRez every hour. Add
        address and house-guide details directly in this template if you want them to
        show every time.
      </div>
    </div>
  </div>

  <div class="footer">{property_name_upper} · GUEST WELCOME</div>
</div>
<script>
  // Belt-and-suspenders auto-refresh: the <meta refresh> tag above should
  // handle this, but some kiosk/tablet browsers ignore meta refresh
  // entirely. This JS timer forces a hard reload with a cache-busting
  // query param so it can't just re-show a cached copy of this page.
  setTimeout(function() {{
    window.location.href = window.location.pathname + "?_=" + Date.now();
  }}, 3600000); // 1 hour
</script>
</body>
</html>
"""

ERROR_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Welcome screen — error</title>
<meta http-equiv="refresh" content="300">
<style>
body{{font-family:sans-serif;background:#F6F1E1;color:#2A2018;padding:60px;}}
h1{{color:#C1652F;}}
</style></head>
<body>
<h1>Couldn't refresh from OwnerRez</h1>
<p>{error}</p>
<p>Last successful update: {last_updated}</p>
<p>This page will retry automatically.</p>
<script>
  setTimeout(function() {{
    window.location.href = window.location.pathname + "?_=" + Date.now();
  }}, 300000); // 5 minutes, matching the meta refresh above
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# 3. GUEST SELECTION + BACKGROUND REFRESH LOOP
# ---------------------------------------------------------------------------
def _recompute_selected_and_render():
    """
    Picks which guest to show on "/" based on the current mode, and
    re-renders the cached HTML for it. Must be called with _cache_lock
    already held (it's an RLock, so nested acquisition inside callers
    that already hold it is safe).
    """
    with _cache_lock:
        upcoming = _cache["upcoming"]
        if not upcoming:
            return  # nothing to render yet / API returned nothing

        selected = None
        if _cache["mode"] == "manual" and _cache["selected_key"]:
            selected = next(
                (g for g in upcoming if g["booking_key"] == _cache["selected_key"]),
                None,
            )
            if selected is None:
                # The manually-pinned guest is no longer in the upcoming
                # list (they checked out, or the booking was cancelled)
                # -- fall back to auto rather than show nothing.
                _cache["mode"] = "auto"

        if selected is None:
            selected = upcoming[0]
            _cache["selected_key"] = selected["booking_key"]

        _cache["html"] = render_html(selected)


def get_task_states(booking_key):
    """Returns {task_name: bool} for every current task, defaulting
    unseen tasks to False -- so editing CLEANING_TASKS later just adds
    new unchecked items instead of breaking existing saved state."""
    with _cache_lock:
        saved = _cache["cleaning"].get(booking_key, {})
        return {task: saved.get(task, False) for task in CLEANING_TASKS}


def toggle_task(booking_key, task_name):
    if task_name not in CLEANING_TASKS:
        return
    with _cache_lock:
        state = _cache["cleaning"].setdefault(booking_key, {})
        state[task_name] = not state.get(task_name, False)


def reset_tasks(booking_key):
    with _cache_lock:
        _cache["cleaning"][booking_key] = {}


def _prune_cleaning_state():
    """Drops checklist state for bookings no longer in the upcoming
    list (guest arrived/departed, or the booking was cancelled) so this
    dict doesn't grow forever. Must be called with _cache_lock held."""
    valid_keys = {g["booking_key"] for g in _cache["upcoming"]}
    for key in list(_cache["cleaning"].keys()):
        if key not in valid_keys:
            del _cache["cleaning"][key]


def refresh_cache():
    try:
        upcoming = fetch_upcoming_arrivals()
        with _cache_lock:
            _cache["upcoming"] = upcoming
            _prune_cleaning_state()
            _recompute_selected_and_render()
            _cache["last_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _cache["last_error"] = None
        shown = next(
            (g for g in upcoming if g["booking_key"] == _cache["selected_key"]),
            upcoming[0],
        )
        print(f"[{_cache['last_updated']}] Refreshed. {len(upcoming)} upcoming booking(s). "
              f"Showing: {shown['first_name']} (arriving {shown['arrival']}, "
              f"mode={_cache['mode']})")
    except Exception as e:
        with _cache_lock:
            _cache["last_error"] = str(e)
        print(f"[{datetime.datetime.now()}] Refresh FAILED: {e}", file=sys.stderr)


def background_loop():
    while True:
        refresh_cache()
        time.sleep(REFRESH_SECONDS)


# ---------------------------------------------------------------------------
# 4. WEB SERVER
# ---------------------------------------------------------------------------
app = Flask(__name__)


def _no_cache(resp):
    # Prevent the tablet's browser (or any proxy/CDN in between) from
    # caching pages. Without this, a kiosk browser can keep showing a
    # stale copy even after it "reloads" -- it just re-serves cached
    # bytes instead of asking the server for fresh ones.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    with _cache_lock:
        if _cache["last_error"] and _cache["last_updated"] is None:
            # Never had a successful fetch yet
            html = ERROR_TEMPLATE.format(
                error=_cache["last_error"],
                last_updated="never",
            )
        else:
            html = _cache["html"]
    return _no_cache(Response(html, mimetype="text/html"))


@app.route("/status")
def status():
    with _cache_lock:
        return {
            "last_updated": _cache["last_updated"],
            "last_error": _cache["last_error"],
            "mode": _cache["mode"],
            "selected_key": _cache["selected_key"],
            "upcoming_count": len(_cache["upcoming"]),
        }


@app.route("/refresh")
def manual_refresh():
    # Lets you force an immediate refresh by visiting /refresh in a browser,
    # instead of waiting up to an hour.
    refresh_cache()
    with _cache_lock:
        err = _cache["last_error"]
    if err:
        return f"Refresh attempted but failed: {err}", 500
    return "Refreshed. <a href='/'>View welcome screen</a> · <a href='/manage'>Manage</a>"


@app.route("/manage")
def manage():
    with _cache_lock:
        upcoming = list(_cache["upcoming"])
        mode = _cache["mode"]
        selected_key = _cache["selected_key"]
        last_updated = _cache["last_updated"]
        last_error = _cache["last_error"]

    if not upcoming:
        rows_html = (
            '<tr><td colspan="6" class="empty-state">No upcoming bookings loaded yet'
            + (f' — last error: {last_error}' if last_error else '')
            + '.</td></tr>'
        )
    else:
        row_parts = []
        for g in upcoming:
            is_selected = g["booking_key"] == selected_key
            nights = (g["departure"] - g["arrival"]).days
            row_parts.append(MANAGE_ROW_TEMPLATE.format(
                booking_key=g["booking_key"],
                first_name=g["first_name"],
                arrival_str=format_date(g["arrival"]),
                departure_str=format_date(g["departure"]),
                nights=nights,
                nights_label="night" if nights == 1 else "nights",
                adults=g["adults"],
                platform=g["platform"],
                confirmation=g["confirmation"],
                row_class="manage-row-selected" if is_selected else "",
                button_label="Currently showing" if is_selected else "Show this guest",
                button_disabled="disabled" if is_selected else "",
            ))
        rows_html = "".join(row_parts)

    html = MANAGE_TEMPLATE.format(
        rows=rows_html,
        mode_auto_class="mode-active" if mode == "auto" else "",
        mode_manual_class="mode-active" if mode == "manual" else "",
        last_updated=last_updated or "never",
        upcoming_count_label=len(upcoming),
        error_banner=(
            f'<div class="error-banner">Last refresh failed: {last_error}</div>'
            if last_error else ""
        ),
    )
    return _no_cache(Response(html, mimetype="text/html"))


@app.route("/manage/mode", methods=["POST"])
def set_mode():
    mode = request.form.get("mode")
    if mode not in ("auto", "manual"):
        return "Invalid mode", 400
    with _cache_lock:
        _cache["mode"] = mode
        _recompute_selected_and_render()
    return redirect("/manage")


@app.route("/manage/select", methods=["POST"])
def select_guest():
    booking_key = request.form.get("booking_key")
    if not booking_key:
        return "Missing booking_key", 400
    with _cache_lock:
        _cache["mode"] = "manual"
        _cache["selected_key"] = booking_key
        _recompute_selected_and_render()
    return redirect("/manage")


@app.route("/cleaning")
def cleaning():
    with _cache_lock:
        upcoming = list(_cache["upcoming"])
        last_updated = _cache["last_updated"]
        last_error = _cache["last_error"]
        # Snapshot task states for each booking while still holding the lock
        states = {g["booking_key"]: get_task_states(g["booking_key"]) for g in upcoming}

    if not upcoming:
        cards_html = (
            '<p class="empty-state">No upcoming bookings loaded yet'
            + (f' — last error: {last_error}' if last_error else '')
            + '.</p>'
        )
    else:
        cards = []
        for g in upcoming:
            task_state = states[g["booking_key"]]
            done_count = sum(1 for v in task_state.values() if v)
            total = len(CLEANING_TASKS)
            all_done = done_count == total

            checkbox_rows = "".join(
                CLEANING_CHECKBOX_TEMPLATE.format(
                    booking_key=g["booking_key"],
                    task_name=task_name,
                    checked="checked" if checked else "",
                    done_class="task-done" if checked else "",
                )
                for task_name, checked in task_state.items()
            )

            cards.append(CLEANING_CARD_TEMPLATE.format(
                booking_key=g["booking_key"],
                first_name=g["first_name"],
                arrival_str=format_date(g["arrival"]),
                departure_str=format_date(g["departure"]),
                done_count=done_count,
                total=total,
                progress_pct=int(100 * done_count / total) if total else 0,
                ready_badge='<span class="ready-badge">Ready ✓</span>' if all_done else "",
                card_class="cleaning-card-done" if all_done else "",
                checkbox_rows=checkbox_rows,
            ))
        cards_html = "".join(cards)

    html = CLEANING_TEMPLATE.format(
        cards=cards_html,
        last_updated=last_updated or "never",
        error_banner=(
            f'<div class="error-banner">Last refresh failed: {last_error}</div>'
            if last_error else ""
        ),
    )
    return _no_cache(Response(html, mimetype="text/html"))


@app.route("/cleaning/toggle", methods=["POST"])
def cleaning_toggle():
    booking_key = request.form.get("booking_key")
    task_name = request.form.get("task_name")
    if not booking_key or not task_name:
        return "Missing booking_key or task_name", 400
    toggle_task(booking_key, task_name)
    return redirect("/cleaning#" + booking_key)


@app.route("/cleaning/reset", methods=["POST"])
def cleaning_reset():
    booking_key = request.form.get("booking_key")
    if not booking_key:
        return "Missing booking_key", 400
    reset_tasks(booking_key)
    return redirect("/cleaning#" + booking_key)


MANAGE_ROW_TEMPLATE = """
      <tr class="{row_class}">
        <td class="guest-name">{first_name}</td>
        <td>{arrival_str}</td>
        <td>{departure_str}</td>
        <td>{nights} {nights_label}</td>
        <td>{adults} adults</td>
        <td>{platform}<br><span class="conf">{confirmation}</span></td>
        <td>
          <form method="POST" action="/manage/select">
            <input type="hidden" name="booking_key" value="{booking_key}">
            <button type="submit" class="select-btn" {button_disabled}>{button_label}</button>
          </form>
        </td>
      </tr>
"""

MANAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Manage — Villa Brushy Creek Welcome Screen</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,500;9..144,600&family=Work+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{{
    --creek: #1F3F3D; --creek-deep: #142B29; --limestone: #EFEAD9;
    --sage: #7C8B65; --clay: #C1652F; --bark: #2A2018;
  }}
  *{{box-sizing:border-box;}}
  body{{ margin:0; background: var(--limestone); color: var(--bark);
    font-family:'Work Sans', sans-serif; padding: 40px 32px 60px; }}
  .wrap{{ max-width: 980px; margin:0 auto; }}
  h1{{ font-family:'Fraunces', serif; font-weight:500; font-size: 34px;
    color: var(--creek-deep); margin: 0 0 6px; }}
  .subtitle{{ color:#77705C; margin: 0 0 28px; font-size:14px; }}
  .back-link{{ font-size: 13px; color: var(--creek); text-decoration:none; }}
  .back-link:hover{{ text-decoration:underline; }}
  .error-banner{{
    background:#FBEAE0; border:1px solid #E8B99B; color:#8A3D14;
    padding:12px 16px; border-radius:10px; margin-bottom:20px; font-size:14px;
  }}
  .mode-toggle{{ display:flex; gap:10px; margin: 20px 0 28px; }}
  .mode-toggle form{{ margin:0; }}
  .mode-btn{{
    font-family:'Work Sans', sans-serif; font-size:14px; font-weight:500;
    padding: 10px 20px; border-radius: 100px; border:1px solid #DCD4B8;
    background:#fff; color: var(--bark); cursor:pointer;
  }}
  .mode-btn.mode-active{{
    background: var(--creek); color: var(--limestone); border-color: var(--creek);
  }}
  .mode-explainer{{ font-size: 13px; color:#77705C; margin-bottom: 28px; }}
  table{{ width:100%; border-collapse: collapse; background:#fff;
    border-radius: 14px; overflow:hidden; border:1px solid #E2DBC5; }}
  th{{
    text-align:left; font-size: 11px; text-transform:uppercase; letter-spacing:0.08em;
    color:#8A7F63; padding: 14px 16px; border-bottom: 1px solid #E2DBC5; background:#FAF6E9;
  }}
  td{{ padding: 14px 16px; border-bottom: 1px solid #EFEAD9; font-size: 14px; vertical-align: middle; }}
  tr:last-child td{{ border-bottom:none; }}
  .guest-name{{ font-family:'Fraunces', serif; font-weight:500; font-size: 16px; color: var(--creek-deep); }}
  .conf{{ color:#9A9276; font-size:12px; }}
  .manage-row-selected{{ background: #F3F0DD; }}
  .select-btn{{
    font-family:'Work Sans', sans-serif; font-size: 13px; font-weight:500;
    padding: 8px 14px; border-radius: 100px; border:1px solid var(--clay);
    background:#fff; color: var(--clay); cursor:pointer; white-space:nowrap;
  }}
  .select-btn:hover{{ background: var(--clay); color:#fff; }}
  .select-btn[disabled]{{
    border-color:#DCD4B8; color:#9A9276; cursor:default; background:#F3F0DD;
  }}
  .empty-state{{ text-align:center; color:#9A9276; padding: 30px; }}
  .footer-note{{ margin-top: 24px; font-size: 12px; color:#9A9276; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back-link" href="/">&larr; Back to welcome screen</a> · <a class="back-link" href="/cleaning">Cleaning checklist &rarr;</a>
  <h1>Upcoming Arrivals</h1>
  <p class="subtitle">Last refreshed: {last_updated} · <a class="back-link" href="/refresh">Refresh now</a></p>
  {error_banner}

  <div class="mode-toggle">
    <form method="POST" action="/manage/mode">
      <input type="hidden" name="mode" value="auto">
      <button type="submit" class="mode-btn {mode_auto_class}">Auto</button>
    </form>
    <form method="POST" action="/manage/mode">
      <input type="hidden" name="mode" value="manual">
      <button type="submit" class="mode-btn {mode_manual_class}">Manual</button>
    </form>
  </div>
  <p class="mode-explainer">
    <b>Auto</b> always shows whoever's arriving soonest.
    <b>Manual</b> keeps showing whichever guest you pick below, even after the
    hourly refresh, until you pick someone else or switch back to Auto.
  </p>

  <table>
    <thead>
      <tr>
        <th>Guest</th><th>Arrival</th><th>Departure</th><th>Stay</th>
        <th>Party</th><th>Booking</th><th></th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>

  <p class="footer-note">Showing the next {upcoming_count_label} upcoming bookings. This page has no login — don't share the URL publicly.</p>
</div>
</body>
</html>
"""


CLEANING_CHECKBOX_TEMPLATE = """
        <li class="task-row {done_class}">
          <form method="POST" action="/cleaning/toggle">
            <input type="hidden" name="booking_key" value="{booking_key}">
            <input type="hidden" name="task_name" value="{task_name}">
            <label class="task-label">
              <input type="checkbox" {checked} onchange="this.form.submit()">
              <span>{task_name}</span>
            </label>
          </form>
        </li>
"""

CLEANING_CARD_TEMPLATE = """
  <div class="cleaning-card {card_class}" id="{booking_key}">
    <div class="cleaning-card-header">
      <div>
        <div class="cleaning-guest">{first_name}</div>
        <div class="cleaning-dates">Arrives {arrival_str} · Departs {departure_str}</div>
      </div>
      <div class="cleaning-progress-wrap">
        {ready_badge}
        <div class="cleaning-progress-label">{done_count}/{total} done</div>
        <div class="progress-bar"><div class="progress-fill" style="width:{progress_pct}%"></div></div>
      </div>
    </div>
    <ul class="task-list">
      {checkbox_rows}
    </ul>
    <form method="POST" action="/cleaning/reset">
      <input type="hidden" name="booking_key" value="{booking_key}">
      <button type="submit" class="reset-btn">Reset checklist</button>
    </form>
  </div>
"""

CLEANING_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Cleaning Checklist — Villa Brushy Creek</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,500;9..144,600&family=Work+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{{
    --creek: #1F3F3D; --creek-deep: #142B29; --limestone: #EFEAD9;
    --sage: #7C8B65; --clay: #C1652F; --bark: #2A2018;
  }}
  *{{box-sizing:border-box;}}
  body{{ margin:0; background: var(--limestone); color: var(--bark);
    font-family:'Work Sans', sans-serif; padding: 40px 32px 60px; }}
  .wrap{{ max-width: 900px; margin:0 auto; }}
  h1{{ font-family:'Fraunces', serif; font-weight:500; font-size: 34px;
    color: var(--creek-deep); margin: 0 0 6px; }}
  .subtitle{{ color:#77705C; margin: 0 0 28px; font-size:14px; }}
  .back-link{{ font-size: 13px; color: var(--creek); text-decoration:none; }}
  .back-link:hover{{ text-decoration:underline; }}
  .error-banner{{
    background:#FBEAE0; border:1px solid #E8B99B; color:#8A3D14;
    padding:12px 16px; border-radius:10px; margin-bottom:20px; font-size:14px;
  }}
  .empty-state{{ text-align:center; color:#9A9276; padding: 30px; }}
  .cleaning-card{{
    background:#fff; border:1px solid #E2DBC5; border-radius:16px;
    padding: 22px 24px; margin-bottom: 20px;
  }}
  .cleaning-card-done{{ border-color: var(--sage); background: #F7F9F2; }}
  .cleaning-card-header{{
    display:flex; justify-content:space-between; align-items:flex-start;
    gap: 20px; flex-wrap: wrap; margin-bottom: 16px;
  }}
  .cleaning-guest{{ font-family:'Fraunces', serif; font-weight:500; font-size: 20px; color: var(--creek-deep); }}
  .cleaning-dates{{ font-size: 13px; color:#77705C; margin-top: 2px; }}
  .cleaning-progress-wrap{{ text-align:right; min-width: 160px; }}
  .ready-badge{{
    display:inline-block; background: var(--sage); color:#fff; font-size: 12px;
    font-weight:600; padding: 3px 10px; border-radius: 100px; margin-bottom:6px;
  }}
  .cleaning-progress-label{{ font-size: 12px; color:#8A7F63; margin-bottom: 6px; }}
  .progress-bar{{ width: 160px; height: 6px; background:#EFEAD9; border-radius: 100px; overflow:hidden; }}
  .progress-fill{{ height:100%; background: var(--clay); transition: width 0.2s ease; }}
  .cleaning-card-done .progress-fill{{ background: var(--sage); }}
  .task-list{{ list-style:none; margin: 0 0 16px; padding:0; border-top:1px solid #EFEAD9; }}
  .task-row{{ border-bottom: 1px solid #EFEAD9; }}
  .task-row form{{ margin:0; }}
  .task-label{{
    display:flex; align-items:center; gap: 12px; padding: 11px 2px;
    font-size: 14px; cursor:pointer;
  }}
  .task-label input[type=checkbox]{{ width:18px; height:18px; accent-color: var(--sage); cursor:pointer; }}
  .task-row.task-done .task-label span{{ color:#9A9276; text-decoration: line-through; }}
  .reset-btn{{
    font-family:'Work Sans', sans-serif; font-size: 12px; color:#8A7F63;
    background:none; border:1px solid #DCD4B8; border-radius:100px;
    padding: 6px 14px; cursor:pointer;
  }}
  .reset-btn:hover{{ border-color: var(--clay); color: var(--clay); }}
  .footer-note{{ margin-top: 8px; font-size: 12px; color:#9A9276; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back-link" href="/">&larr; Back to welcome screen</a> · <a class="back-link" href="/manage">Manage arrivals &rarr;</a>
  <h1>Cleaning Checklist</h1>
  <p class="subtitle">Last refreshed: {last_updated} · <a class="back-link" href="/refresh">Refresh now</a></p>
  {error_banner}

  {cards}

  <p class="footer-note">Checklists are per booking and reset automatically once a booking is no longer upcoming. Checking a box saves immediately — no need to submit anything. This page has no login — don't share the URL publicly.</p>
</div>
</body>
</html>
"""


# Start the background refresh loop as soon as the module is imported.
# This runs whether the app is launched via `python app.py` (dev) or
# via gunicorn (Render/production), since gunicorn imports this module
# and never executes the `if __name__ == "__main__"` block below.
threading.Thread(target=background_loop, daemon=True).start()

if __name__ == "__main__":
    # Local dev only. On Render, gunicorn runs this instead (see
    # requirements.txt / start command in the deploy notes above) --
    # Flask's built-in server here isn't meant for production traffic.
    print(f"Starting server on http://0.0.0.0:{PORT}  (Ctrl+C to stop)")
    app.run(host="0.0.0.0", port=PORT)
