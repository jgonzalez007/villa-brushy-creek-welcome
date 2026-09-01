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
import uuid
import json
import sqlite3
import asyncio
import threading
import time
import datetime
from zoneinfo import ZoneInfo
import requests
from flask import Flask, Response, request, redirect

import kwikset_client

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

# SQLite persistence. DB_PATH should point inside a Render Persistent
# Disk's mount path (e.g. /var/data/app.db) so data survives deploys and
# restarts -- without a disk attached, this still works but the file
# lives on the service's ephemeral filesystem and is lost on every
# deploy, same as the old in-memory-only behavior. See README for how
# to attach a disk.
DB_PATH = os.environ.get("DB_PATH", "./data/app.db")


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

# Pool control via iAqualink (Jandy/Zodiac). Uses the `iaqualink` PyPI
# package (an unofficial, reverse-engineered client library -- Jandy/
# Zodiac/Fluidra don't publish an official API). Requires Python 3.14+
# (see render.yaml PYTHON_VERSION). If these aren't set, /pool shows a
# "not configured" message instead of erroring.
IAQUALINK_USERNAME = os.environ.get("IAQUALINK_USERNAME")
IAQUALINK_PASSWORD = os.environ.get("IAQUALINK_PASSWORD")

# Pool equipment scheduler. Timezone matters because schedule times are
# entered as local wall-clock times (e.g. "8:00 AM") -- without a fixed
# timezone, the server's own clock (UTC on Render) would fire schedules
# at the wrong local time. Cedar Park, TX is Central time.
POOL_TIMEZONE = os.environ.get("POOL_TIMEZONE", "America/Chicago")
POOL_SCHEDULE_CHECK_SECONDS = 30  # how often the scheduler loop checks for due triggers

# Kwikset door-code sending. Login itself (Cognito SRP + a two-round phone
# verification challenge) is NOT reimplemented here -- run auth-setup.js
# from the kwikset-mcp-node project once, by hand, on any machine, and
# put the resulting email + refresh token here. Everything from that
# point on (token refresh, REST calls) is handled by this app.
KWIKSET_EMAIL = os.environ.get("KWIKSET_EMAIL")
KWIKSET_REFRESH_TOKEN = os.environ.get("KWIKSET_REFRESH_TOKEN")

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
    # Pool equipment schedules: schedule_id -> {device_key, device_label,
    # on_time ("HH:MM"), off_time ("HH:MM"), days (list of 0=Mon..6=Sun),
    # enabled (bool), last_triggered_on/_off (date string, prevents firing
    # more than once per day for the same trigger)}.
    "pool_schedules": {},
}


# ---------------------------------------------------------------------------
# 0. DATABASE (SQLite persistence)
# ---------------------------------------------------------------------------
# Persists pool schedules, cleaning checklist progress, and guest
# selection mode so they survive deploys/restarts -- previously all of
# this lived in memory only and was wiped on every redeploy. One
# connection, reused across threads (SQLite handles this fine for a
# single-writer, low-traffic app like this), all access serialized
# through _cache_lock since that's already held during every mutation.
_db_conn = None


def get_db():
    global _db_conn
    if _db_conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
    return _db_conn


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pool_schedules (
            id TEXT PRIMARY KEY,
            device_key TEXT NOT NULL,
            device_label TEXT NOT NULL,
            on_time TEXT NOT NULL,
            off_time TEXT NOT NULL,
            days TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            last_triggered_on TEXT,
            last_triggered_off TEXT
        );
        CREATE TABLE IF NOT EXISTS cleaning_state (
            booking_key TEXT NOT NULL,
            task_name TEXT NOT NULL,
            done INTEGER NOT NULL,
            PRIMARY KEY (booking_key, task_name)
        );
        CREATE TABLE IF NOT EXISTS guest_selection (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            mode TEXT NOT NULL,
            selected_key TEXT
        );
        CREATE TABLE IF NOT EXISTS kwikset_auth (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            email TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS kwikset_access_codes (
            device_id TEXT NOT NULL,
            slot INTEGER NOT NULL,
            booking_key TEXT,
            guest_name TEXT,
            code TEXT,
            schedule_json TEXT,
            created_at TEXT,
            PRIMARY KEY (device_id, slot)
        );
    """)
    db.commit()

    # Seed the Kwikset refresh token from env vars on first run only --
    # after that, the database row is authoritative (and self-updates if
    # Cognito ever rotates the refresh token on a future refresh call).
    if KWIKSET_EMAIL and KWIKSET_REFRESH_TOKEN:
        existing = db.execute("SELECT 1 FROM kwikset_auth WHERE id = 1").fetchone()
        if not existing:
            db.execute(
                "INSERT INTO kwikset_auth (id, email, refresh_token, updated_at) VALUES (1, ?, ?, ?)",
                (KWIKSET_EMAIL, KWIKSET_REFRESH_TOKEN, datetime.datetime.now().isoformat()),
            )
            db.commit()


def db_load_all():
    """Called once at startup to repopulate _cache from disk. Returns a
    dict the caller merges into _cache -- doesn't touch _cache directly
    so this stays easily testable in isolation."""
    db = get_db()

    schedules = {}
    for row in db.execute("SELECT * FROM pool_schedules"):
        schedules[row["id"]] = {
            "device_key": row["device_key"],
            "device_label": row["device_label"],
            "on_time": row["on_time"],
            "off_time": row["off_time"],
            "days": [int(d) for d in row["days"].split(",") if d != ""],
            "enabled": bool(row["enabled"]),
            "last_triggered_on": row["last_triggered_on"],
            "last_triggered_off": row["last_triggered_off"],
        }

    cleaning = {}
    for row in db.execute("SELECT * FROM cleaning_state"):
        cleaning.setdefault(row["booking_key"], {})[row["task_name"]] = bool(row["done"])

    mode, selected_key = "auto", None
    sel_row = db.execute("SELECT mode, selected_key FROM guest_selection WHERE id = 1").fetchone()
    if sel_row:
        mode, selected_key = sel_row["mode"], sel_row["selected_key"]

    return {
        "pool_schedules": schedules,
        "cleaning": cleaning,
        "mode": mode,
        "selected_key": selected_key,
    }


def db_save_pool_schedule(schedule_id, sched):
    db = get_db()
    db.execute(
        """INSERT INTO pool_schedules
           (id, device_key, device_label, on_time, off_time, days, enabled,
            last_triggered_on, last_triggered_off)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             device_key=excluded.device_key, device_label=excluded.device_label,
             on_time=excluded.on_time, off_time=excluded.off_time,
             days=excluded.days, enabled=excluded.enabled,
             last_triggered_on=excluded.last_triggered_on,
             last_triggered_off=excluded.last_triggered_off""",
        (schedule_id, sched["device_key"], sched["device_label"], sched["on_time"],
         sched["off_time"], ",".join(str(d) for d in sched["days"]), int(sched["enabled"]),
         sched["last_triggered_on"], sched["last_triggered_off"]),
    )
    db.commit()


def db_delete_pool_schedule(schedule_id):
    db = get_db()
    db.execute("DELETE FROM pool_schedules WHERE id = ?", (schedule_id,))
    db.commit()


def db_save_cleaning_task(booking_key, task_name, done):
    db = get_db()
    db.execute(
        """INSERT INTO cleaning_state (booking_key, task_name, done)
           VALUES (?, ?, ?)
           ON CONFLICT(booking_key, task_name) DO UPDATE SET done=excluded.done""",
        (booking_key, task_name, int(done)),
    )
    db.commit()


def db_clear_cleaning_booking(booking_key):
    db = get_db()
    db.execute("DELETE FROM cleaning_state WHERE booking_key = ?", (booking_key,))
    db.commit()


def db_save_guest_selection(mode, selected_key):
    db = get_db()
    db.execute(
        """INSERT INTO guest_selection (id, mode, selected_key) VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, selected_key=excluded.selected_key""",
        (mode, selected_key),
    )
    db.commit()


def db_load_kwikset_auth():
    db = get_db()
    row = db.execute("SELECT email, refresh_token FROM kwikset_auth WHERE id = 1").fetchone()
    if not row:
        return None
    return {"email": row["email"], "refresh_token": row["refresh_token"]}


def db_save_kwikset_auth(email, refresh_token):
    db = get_db()
    db.execute(
        """INSERT INTO kwikset_auth (id, email, refresh_token, updated_at) VALUES (1, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET email=excluded.email, refresh_token=excluded.refresh_token,
             updated_at=excluded.updated_at""",
        (email, refresh_token, datetime.datetime.now().isoformat()),
    )
    db.commit()


def db_record_access_code(device_id, slot, booking_key, guest_name, code, schedule):
    db = get_db()
    db.execute(
        """INSERT INTO kwikset_access_codes
           (device_id, slot, booking_key, guest_name, code, schedule_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(device_id, slot) DO UPDATE SET
             booking_key=excluded.booking_key, guest_name=excluded.guest_name,
             code=excluded.code, schedule_json=excluded.schedule_json,
             created_at=excluded.created_at""",
        (device_id, slot, booking_key, guest_name, code, json.dumps(schedule), datetime.datetime.now().isoformat()),
    )
    db.commit()


def db_next_access_code_slot(device_id):
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(MAX(slot), 0) + 1 AS next_slot FROM kwikset_access_codes WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    return row["next_slot"]


def db_find_access_code_for_booking(device_id, booking_key):
    db = get_db()
    return db.execute(
        "SELECT * FROM kwikset_access_codes WHERE device_id = ? AND booking_key = ?",
        (device_id, booking_key),
    ).fetchone()


def _db_safe(fn, *args, **kwargs):
    """Wraps a db_* write call so a database hiccup degrades gracefully
    (in-memory state still works, matching this app's existing philosophy
    everywhere else) instead of crashing the feature that triggered it."""
    try:
        fn(*args, **kwargs)
    except Exception as e:
        print(f"[{datetime.datetime.now()}] Database write failed "
              f"({fn.__name__}): {e}", file=sys.stderr)


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
    guest_last_name = b.get("guest", {}).get("last_name", "")
    guest_id = b.get("guest", {}).get("id") or b.get("guest_id")
    property_name = b.get("property", {}).get("name", PROPERTY_DISPLAY_NAME)
    return {
        "booking_key": _booking_key(b),
        "first_name": guest_first_name,
        "last_name": guest_last_name,
        "guest_id": guest_id,
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


def _fetch_all_bookings():
    """Paginated fetch of every booking on the account (any status, any
    date). Shared by fetch_upcoming_arrivals and fetch_bookings_for_month
    -- both need the full set since OwnerRez's API has no server-side
    arrival-date filter on this endpoint (see comment below)."""
    if not OWNERREZ_USERNAME or not OWNERREZ_TOKEN:
        raise RuntimeError(
            "Missing credentials. Set OWNERREZ_USERNAME and OWNERREZ_TOKEN "
            "environment variables before running this script."
        )

    headers = {
        "User-Agent": "Villa Brushy Creek Welcome Screen/1.0",
        "Accept": "application/json",
    }

    # OwnerRez's v2 /bookings endpoint only supports since_utc, which
    # filters by *last-changed* date, not arrival/stay date -- there is
    # no server-side arrival filter on this endpoint. So we ask for
    # everything changed since a distant date (effectively "all
    # bookings"), paginate through results, and filter ourselves.
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
        page = payload.get("items") or payload.get("bookings") or []
        all_bookings.extend(page)
        if len(page) < page_size:
            break  # last page
        offset += page_size
        if offset > 2000:
            break  # safety cap against runaway pagination

    return all_bookings


def fetch_upcoming_arrivals(limit=UPCOMING_COUNT):
    """
    Returns a list of up to `limit` guest dicts for the soonest upcoming
    active bookings, soonest arrival first.
    """
    today = datetime.date.today()
    all_bookings = _fetch_all_bookings()

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


def fetch_bookings_for_month(year, month):
    """Returns every real guest booking (active, not a block/owner stay)
    whose arrival falls within the given calendar month, sorted by
    arrival date. Used by /doors' month picker."""
    all_bookings = _fetch_all_bookings()

    matching = []
    for b in all_bookings:
        if b.get("status") != "active" or b.get("is_block") or "arrival" not in b:
            continue
        if not b.get("guest"):
            continue  # blocks/owner stays have no guest object
        arrival = datetime.date.fromisoformat(b["arrival"])
        if arrival.year == year and arrival.month == month:
            matching.append(b)

    matching.sort(key=lambda b: b["arrival"])
    return [_booking_to_guest_dict(b) for b in matching]


def fetch_guest_phone(guest_id):
    """Looks up a guest's phone number -- NOT included in the booking
    list itself, only via a separate guest-detail lookup. Returns the
    raw phone string, or None if unavailable. Best-effort: callers should
    tolerate None rather than let one guest's lookup failure break a
    whole page of other guests."""
    if not OWNERREZ_USERNAME or not OWNERREZ_TOKEN:
        return None
    headers = {
        "User-Agent": "Villa Brushy Creek Welcome Screen/1.0",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(
            f"{API_BASE}/guests/{guest_id}",
            auth=(OWNERREZ_USERNAME, OWNERREZ_TOKEN),
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        guest = resp.json()
    except Exception as e:
        print(f"[{datetime.datetime.now()}] Guest phone lookup failed for "
              f"guest_id={guest_id}: {e}", file=sys.stderr)
        return None

    phones = guest.get("phones") or []
    if not phones:
        return None
    default_phone = next((p for p in phones if p.get("is_default")), phones[0])
    return default_phone.get("number")


def phone_last4(phone_number):
    """'+1 620-899-8308' -> '8308'. Returns None if there aren't at least
    4 digits to work with."""
    if not phone_number:
        return None
    digits = "".join(c for c in phone_number if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


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

    Does NOT persist mode/selected_key to the database itself (except
    for the stale-pin fallback below) -- this runs on every single page
    load via refresh_cache(), and callers that intentionally change the
    selection (the /manage routes) already mutate _cache before calling
    this, so a before/after diff here can't detect their change. Those
    routes persist explicitly instead; see set_mode()/select_guest().
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
                # -- fall back to auto rather than show nothing. This is
                # a real, unprompted state change (not triggered by a
                # route), so persist it here -- otherwise a restart
                # would try to re-honor a pin that no longer makes sense.
                _cache["mode"] = "auto"
                _db_safe(db_save_guest_selection, "auto", None)

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
        _db_safe(db_save_cleaning_task, booking_key, task_name, state[task_name])


def reset_tasks(booking_key):
    with _cache_lock:
        _cache["cleaning"][booking_key] = {}
        _db_safe(db_clear_cleaning_booking, booking_key)


def _prune_cleaning_state():
    """Drops checklist state for bookings no longer in the upcoming
    list (guest arrived/departed, or the booking was cancelled) so this
    dict doesn't grow forever. Must be called with _cache_lock held."""
    valid_keys = {g["booking_key"] for g in _cache["upcoming"]}
    for key in list(_cache["cleaning"].keys()):
        if key not in valid_keys:
            del _cache["cleaning"][key]
            _db_safe(db_clear_cleaning_booking, key)


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
# 4. POOL CONTROL (iAqualink)
# ---------------------------------------------------------------------------
# Unlike the OwnerRez data above, pool state is fetched live on every
# /pool page load rather than cached on an hourly timer -- it's a control
# panel a host checks occasionally, not a guest-facing display, and
# pump/heater state can change at any moment (including from the
# official iAqualink app), so a stale hourly cache would be actively
# misleading here.

# Sensors: read-only numeric readouts (skip if empty -- some accounts
# don't have every sensor, e.g. no salt system or no spa).
_POOL_SENSOR_KEYS = {"pool_temp", "spa_temp", "air_temp", "pool_salinity", "spa_salinity", "orp", "ph"}
# Diagnostic-only fields that aren't a real control or a useful readout.
_POOL_SKIP_KEYS = {"is_icl_present", "relay_count"}


async def _pool_with_client(fn):
    try:
        from iaqualink.client import AqualinkClient
    except ImportError as e:
        # Surface the REAL import error instead of a generic guess -- it
        # could be a missing package, but it could also be a version
        # mismatch or the class living in a different submodule than
        # expected.
        raise RuntimeError(
            f"Couldn't import AqualinkClient: {type(e).__name__}: {e}. "
            "Check the Render Shell to confirm where AqualinkClient actually "
            "lives in the installed iaqualink package."
        )
    if not IAQUALINK_USERNAME or not IAQUALINK_PASSWORD:
        raise RuntimeError(
            "Missing credentials. Set IAQUALINK_USERNAME and IAQUALINK_PASSWORD "
            "environment variables before using pool control."
        )
    async with AqualinkClient(IAQUALINK_USERNAME, IAQUALINK_PASSWORD) as client:
        systems = await client.get_systems()
        if not systems:
            raise RuntimeError("No pool/spa systems found on this iAqualink account.")
        system = list(systems.values())[0]  # this account has exactly one system
        return await fn(system)


def _device_to_dict(key, device):
    return {
        "key": key,
        "label": getattr(device, "label", None) or key.replace("_", " ").title(),
        "state": getattr(device, "state", "") or "",
        "is_on": getattr(device, "is_on", None),
    }


async def _fetch_pool_snapshot():
    async def inner(system):
        devices = await system.get_devices()
        device_dicts = {k: _device_to_dict(k, v) for k, v in devices.items()}
        return {
            "system_name": getattr(system, "name", PROPERTY_DISPLAY_NAME + " Pool"),
            "online": getattr(system, "online", None),
            "devices": device_dicts,
        }
    return await _pool_with_client(inner)


async def _toggle_pool_device(device_key):
    async def inner(system):
        devices = await system.get_devices()
        device = devices.get(device_key)
        if device is None:
            raise RuntimeError(f"Unknown pool device: {device_key}")
        # The iaqualink library doesn't expose a public toggle() method --
        # only turn_on()/turn_off() (there's a private _toggle(), but
        # leading-underscore methods aren't part of the public API and
        # shouldn't be called directly). So we implement toggle ourselves
        # based on current state.
        if getattr(device, "is_on", False):
            await device.turn_off()
        else:
            await device.turn_on()
    await _pool_with_client(inner)


async def _set_pool_temperature(device_key, temperature):
    async def inner(system):
        devices = await system.get_devices()
        device = devices.get(device_key)
        if device is None:
            raise RuntimeError(f"Unknown pool device: {device_key}")
        await device.set_temperature(temperature)
    await _pool_with_client(inner)


def get_pool_snapshot():
    """Synchronous wrapper -- Flask routes are sync, iaqualink is async."""
    return asyncio.run(_fetch_pool_snapshot())


def toggle_pool_device(device_key):
    asyncio.run(_toggle_pool_device(device_key))


def set_pool_temperature(device_key, temperature):
    # The library requires a real int (it does `temperature not in
    # range(low, high + 1)` internally) -- a float like 72.0 risks being
    # sent to iAqualink's API as the string "72.0" instead of "72".
    # Parse leniently (a form field might contain "72" or "72.0") but
    # always send a genuine int.
    asyncio.run(_set_pool_temperature(device_key, round(float(temperature))))


async def _set_pool_device_power(device_key, on):
    async def inner(system):
        devices = await system.get_devices()
        device = devices.get(device_key)
        if device is None:
            raise RuntimeError(f"Unknown pool device: {device_key}")
        if on:
            await device.turn_on()
        else:
            await device.turn_off()
    await _pool_with_client(inner)


def set_pool_device_power(device_key, on):
    """Direct on/off (not toggle) -- used by the scheduler, which needs
    idempotent behavior: firing an 'on' trigger when the device is
    already on should be a harmless no-op, not flip it off."""
    asyncio.run(_set_pool_device_power(device_key, on))


def classify_pool_devices(devices):
    """Splits the raw device dict into three display groups: read-only
    sensors, adjustable temperature set points, and on/off equipment."""
    sensors, setpoints, equipment = [], [], []
    for key, d in devices.items():
        if key in _POOL_SKIP_KEYS:
            continue
        if key in _POOL_SENSOR_KEYS:
            if d["state"] != "":
                sensors.append(d)
        elif key.endswith("_set_point"):
            setpoints.append(d)
        elif isinstance(d["is_on"], bool):
            equipment.append(d)
        # else: unavailable/diagnostic field with nothing useful to show
    return sensors, setpoints, equipment


# ---------------------------------------------------------------------------
# 5. POOL SCHEDULER
# ---------------------------------------------------------------------------
# Schedules live in memory only (see the persistence caveat in the README)
# and are checked once every POOL_SCHEDULE_CHECK_SECONDS by a dedicated
# background thread, independent of anyone viewing /pool -- the whole
# point is that "turn the pump on at 8am" fires whether or not a tablet
# or browser happens to be open at that moment.

_WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _format_time_12h_str(hhmm):
    """'08:00' -> '8:00 AM'. Reuses the same logic as format_time_12h
    but that function expects a raw string too, so just delegate."""
    return format_time_12h(hhmm)


def _format_days_label(days):
    days_sorted = sorted(days)
    if days_sorted == list(range(7)):
        return "Every day"
    if days_sorted == [5, 6]:
        return "Weekends"
    if days_sorted == [0, 1, 2, 3, 4]:
        return "Weekdays"
    return ", ".join(_WEEKDAY_LABELS[d] for d in days_sorted)


def add_pool_schedule(device_key, device_label, on_time, off_time, days):
    schedule_id = uuid.uuid4().hex[:8]
    with _cache_lock:
        sched = {
            "device_key": device_key,
            "device_label": device_label,
            "on_time": on_time,
            "off_time": off_time,
            "days": sorted(set(days)),
            "enabled": True,
            "last_triggered_on": None,
            "last_triggered_off": None,
        }
        _cache["pool_schedules"][schedule_id] = sched
        _db_safe(db_save_pool_schedule, schedule_id, sched)
    return schedule_id


def delete_pool_schedule(schedule_id):
    with _cache_lock:
        _cache["pool_schedules"].pop(schedule_id, None)
        _db_safe(db_delete_pool_schedule, schedule_id)


def toggle_pool_schedule_enabled(schedule_id):
    with _cache_lock:
        sched = _cache["pool_schedules"].get(schedule_id)
        if sched:
            sched["enabled"] = not sched["enabled"]
            _db_safe(db_save_pool_schedule, schedule_id, sched)


def run_pool_schedules_once():
    """Checks every schedule against the current local time and fires
    any that are due. Safe to call repeatedly -- each schedule only
    fires once per calendar day per trigger (on/off), tracked via
    last_triggered_on/_off, so calling this every 30s doesn't cause
    repeated firing within the same matching minute."""
    if not IAQUALINK_USERNAME or not IAQUALINK_PASSWORD:
        return  # pool control not configured -- nothing to do

    with _cache_lock:
        schedules = list(_cache["pool_schedules"].items())
    if not schedules:
        return

    now = datetime.datetime.now(ZoneInfo(POOL_TIMEZONE))
    today_str = now.strftime("%Y-%m-%d")
    current_hm = now.strftime("%H:%M")
    weekday = now.weekday()  # Monday=0 .. Sunday=6

    for schedule_id, sched in schedules:
        if not sched["enabled"] or weekday not in sched["days"]:
            continue

        if sched["on_time"] == current_hm and sched["last_triggered_on"] != today_str:
            try:
                set_pool_device_power(sched["device_key"], True)
                print(f"[{now}] Pool schedule: turned ON {sched['device_label']}")
            except Exception as e:
                print(f"[{now}] Pool schedule FAILED to turn on "
                      f"{sched['device_label']}: {e}", file=sys.stderr)
            with _cache_lock:
                if schedule_id in _cache["pool_schedules"]:
                    _cache["pool_schedules"][schedule_id]["last_triggered_on"] = today_str
                    _db_safe(db_save_pool_schedule, schedule_id, _cache["pool_schedules"][schedule_id])

        if sched["off_time"] == current_hm and sched["last_triggered_off"] != today_str:
            try:
                set_pool_device_power(sched["device_key"], False)
                print(f"[{now}] Pool schedule: turned OFF {sched['device_label']}")
            except Exception as e:
                print(f"[{now}] Pool schedule FAILED to turn off "
                      f"{sched['device_label']}: {e}", file=sys.stderr)
            with _cache_lock:
                if schedule_id in _cache["pool_schedules"]:
                    _cache["pool_schedules"][schedule_id]["last_triggered_off"] = today_str
                    _db_safe(db_save_pool_schedule, schedule_id, _cache["pool_schedules"][schedule_id])


def pool_scheduler_loop():
    while True:
        try:
            run_pool_schedules_once()
        except Exception as e:
            print(f"[{datetime.datetime.now()}] Pool scheduler loop error: {e}", file=sys.stderr)
        time.sleep(POOL_SCHEDULE_CHECK_SECONDS)


# ---------------------------------------------------------------------------
# 6. DOOR CODES (Kwikset)
# ---------------------------------------------------------------------------
# Login itself is NOT reimplemented here -- see kwikset_client.py's module
# docstring. This app only ever does token refresh (simple, no SRP) plus
# REST calls, using a refresh token obtained once via auth-setup.js from
# the kwikset-mcp-node project and stored in the database.

def get_kwikset_client():
    """Refreshes the saved Cognito session and returns a ready-to-use
    KwiksetClient, or raises a clear error explaining what to do."""
    auth = db_load_kwikset_auth()
    if not auth:
        raise RuntimeError(
            "Kwikset isn't connected yet. Run auth-setup.js from the "
            "kwikset-mcp-node project once, then set KWIKSET_EMAIL and "
            "KWIKSET_REFRESH_TOKEN (or update the database directly)."
        )
    fresh = kwikset_client.refresh_cognito_tokens(auth["email"], auth["refresh_token"])
    # Cognito doesn't always rotate the refresh token -- only write to the
    # database if it actually changed, to avoid a pointless disk write on
    # every single door-code page load.
    if fresh["refresh_token"] != auth["refresh_token"]:
        _db_safe(db_save_kwikset_auth, fresh["email"], fresh["refresh_token"])
    return kwikset_client.KwiksetClient(id_token=fresh["id_token"])


def build_stay_schedule(guest):
    """A date_range schedule matching the guest's actual stay -- the code
    is only valid from check-in to check-out, not permanently."""
    arrival, departure = guest["arrival"], guest["departure"]
    check_in_h, check_in_m = (int(x) for x in guest["check_in_time"].split(":"))
    check_out_h, check_out_m = (int(x) for x in guest["check_out_time"].split(":"))
    return {
        "type": "date_range",
        "start": {
            "year": arrival.year, "month": arrival.month, "day": arrival.day,
            "hour": check_in_h, "minute": check_in_m,
        },
        "end": {
            "year": departure.year, "month": departure.month, "day": departure.day,
            "hour": check_out_h, "minute": check_out_m,
        },
    }


def send_door_code_for_guest(device_id, guest):
    """Sends a code (last 4 of the guest's phone) valid for exactly their
    stay. Returns the sent code dict. Raises on any failure -- callers
    are expected to catch and show the real error, this is too
    consequential an action to fail silently."""
    phone = fetch_guest_phone(guest["guest_id"]) if guest.get("guest_id") else None
    code = phone_last4(phone)
    if not code:
        raise RuntimeError(
            f"No usable phone number on file for {guest['first_name']} "
            f"{guest['last_name']} -- can't derive a 4-digit code."
        )

    client = get_kwikset_client()
    slot = db_next_access_code_slot(device_id)
    schedule = build_stay_schedule(guest)
    guest_full_name = f"{guest['first_name']} {guest['last_name']}".strip()

    result = client.add_access_code(
        device_id=device_id, name=guest_full_name, code=code, slot=slot, schedule=schedule,
    )
    _db_safe(
        db_record_access_code, device_id, slot, guest["booking_key"], guest_full_name, code, schedule,
    )
    return result


# ---------------------------------------------------------------------------
# 7. WEB SERVER
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
    # Fetch fresh OwnerRez data on every page load, not just once an
    # hour in the background. This matters most right after the JS/meta
    # auto-reload fires on the tablet -- without this, a reload could
    # still show up-to-an-hour-old data even though the page itself just
    # refreshed. The hourly background loop keeps running too, so data
    # stays reasonably current even between visits/reloads.
    refresh_cache()
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
        _db_safe(db_save_guest_selection, _cache["mode"], _cache["selected_key"])
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
        _db_safe(db_save_guest_selection, _cache["mode"], _cache["selected_key"])
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


@app.route("/pool")
def pool():
    error = None
    snapshot = None
    if not IAQUALINK_USERNAME or not IAQUALINK_PASSWORD:
        error = ("Pool control isn't configured yet. Set IAQUALINK_USERNAME and "
                  "IAQUALINK_PASSWORD environment variables to enable this page.")
    else:
        try:
            snapshot = get_pool_snapshot()
        except Exception as e:
            error = str(e)

    if error:
        html = POOL_TEMPLATE.format(
            system_name=PROPERTY_DISPLAY_NAME,
            online_badge="",
            error_banner=f'<div class="error-banner">{error}</div>',
            sensor_cards="",
            setpoint_cards="",
            equipment_cards="",
            schedule_rows='<p class="empty-state">Pool control must be working to manage schedules.</p>',
            device_options="",
        )
        return _no_cache(Response(html, mimetype="text/html"))

    try:
        sensors, setpoints, equipment = classify_pool_devices(snapshot["devices"])

        sensor_html = "".join(
            POOL_SENSOR_CARD_TEMPLATE.format(label=s["label"], value=s["state"])
            for s in sensors
        )
        setpoint_html = "".join(
            POOL_SETPOINT_CARD_TEMPLATE.format(
                key=s["key"],
                label=s["label"],
                value=s["state"] or "—",
                status_label="Heating enabled" if s["is_on"] else "Heating off",
                status_class="status-on" if s["is_on"] else "status-off",
            )
            for s in setpoints
        )
        equipment_html = "".join(
            POOL_EQUIPMENT_CARD_TEMPLATE.format(
                key=e["key"],
                label=e["label"],
                status_label="On" if e["is_on"] else "Off",
                status_class="status-on" if e["is_on"] else "status-off",
                card_class="equipment-card-on" if e["is_on"] else "",
                button_label="Turn off" if e["is_on"] else "Turn on",
            )
            for e in equipment
        )

        # Schedule section: dropdown of real equipment devices to schedule,
        # plus the list of existing schedules.
        device_options = "".join(
            f'<option value="{e["key"]}" data-label="{e["label"]}">{e["label"]}</option>'
            for e in equipment
        )

        with _cache_lock:
            schedules = list(_cache["pool_schedules"].items())
        schedules.sort(key=lambda kv: (kv[1]["device_label"], kv[1]["on_time"]))

        if schedules:
            schedule_rows = "".join(
                POOL_SCHEDULE_ROW_TEMPLATE.format(
                    schedule_id=sid,
                    device_label=s["device_label"],
                    on_time=_format_time_12h_str(s["on_time"]),
                    off_time=_format_time_12h_str(s["off_time"]),
                    days_label=_format_days_label(s["days"]),
                    status_label="Enabled" if s["enabled"] else "Disabled",
                    status_class="status-on" if s["enabled"] else "status-off",
                    row_class="" if s["enabled"] else "schedule-row-disabled",
                    toggle_label="Disable" if s["enabled"] else "Enable",
                )
                for sid, s in schedules
            )
        else:
            schedule_rows = '<p class="empty-state">No schedules set up yet.</p>'

        html = POOL_TEMPLATE.format(
            system_name=snapshot["system_name"],
            online_badge=(
                '<span class="online-badge online-yes">Online</span>' if snapshot["online"]
                else '<span class="online-badge online-no">Offline</span>' if snapshot["online"] is False
                else ""
            ),
            error_banner="",
            sensor_cards=sensor_html or '<p class="empty-state">No sensor readings available.</p>',
            setpoint_cards=setpoint_html,
            equipment_cards=equipment_html or '<p class="empty-state">No controllable equipment found.</p>',
            schedule_rows=schedule_rows,
            device_options=device_options or '<option value="">No equipment available</option>',
        )
    except Exception as e:
        # Belt-and-suspenders: a fetch can succeed but return data shaped
        # slightly differently than expected (e.g. an unexpected device
        # attribute). Show the real error instead of a blank 500 page.
        print(f"[{datetime.datetime.now()}] /pool render error: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        html = POOL_TEMPLATE.format(
            system_name=PROPERTY_DISPLAY_NAME,
            online_badge="",
            error_banner=f'<div class="error-banner">Error building pool page: '
                          f'{type(e).__name__}: {e}</div>',
            sensor_cards="",
            setpoint_cards="",
            equipment_cards="",
            schedule_rows="",
            device_options="",
        )
    return _no_cache(Response(html, mimetype="text/html"))


@app.route("/pool/toggle", methods=["POST"])
def pool_toggle():
    device_key = request.form.get("device_key")
    if not device_key:
        return "Missing device_key", 400
    try:
        toggle_pool_device(device_key)
    except Exception as e:
        return f"Failed to toggle device: {e}", 500
    return redirect("/pool")


@app.route("/pool/set_temperature", methods=["POST"])
def pool_set_temperature():
    device_key = request.form.get("device_key")
    temperature = request.form.get("temperature")
    if not device_key or not temperature:
        return "Missing device_key or temperature", 400
    try:
        set_pool_temperature(device_key, temperature)
    except (ValueError, TypeError):
        return "Invalid temperature value", 400
    except Exception as e:
        return f"Failed to set temperature: {e}", 500
    return redirect("/pool")


@app.route("/pool/schedule/add", methods=["POST"])
def pool_schedule_add():
    device_key = request.form.get("device_key")
    device_label = request.form.get("device_label") or device_key
    on_time = request.form.get("on_time")
    off_time = request.form.get("off_time")
    days = request.form.getlist("days")  # list of "0".."6" strings from checkboxes

    if not device_key or not on_time or not off_time:
        return "Missing device_key, on_time, or off_time", 400
    try:
        days_int = [int(d) for d in days]
        if not all(0 <= d <= 6 for d in days_int):
            raise ValueError
    except ValueError:
        return "Invalid days value", 400
    if not days_int:
        return "Select at least one day", 400
    # Basic HH:MM sanity check (the <input type=time> should already
    # guarantee this, but don't trust client-side validation alone).
    for t in (on_time, off_time):
        try:
            datetime.datetime.strptime(t, "%H:%M")
        except ValueError:
            return f"Invalid time value: {t}", 400

    add_pool_schedule(device_key, device_label, on_time, off_time, days_int)
    return redirect("/pool#schedule")


@app.route("/pool/schedule/delete", methods=["POST"])
def pool_schedule_delete():
    schedule_id = request.form.get("schedule_id")
    if not schedule_id:
        return "Missing schedule_id", 400
    delete_pool_schedule(schedule_id)
    return redirect("/pool#schedule")


@app.route("/pool/schedule/toggle", methods=["POST"])
def pool_schedule_toggle():
    schedule_id = request.form.get("schedule_id")
    if not schedule_id:
        return "Missing schedule_id", 400
    toggle_pool_schedule_enabled(schedule_id)
    return redirect("/pool#schedule")


_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]


def _month_options(selected_year, selected_month):
    """The current month plus the next 5, as (year, month, label, selected) tuples."""
    today = datetime.date.today()
    options = []
    y, m = today.year, today.month
    for _ in range(6):
        label = f"{_MONTH_NAMES[m]} {y}"
        options.append((y, m, label, (y == selected_year and m == selected_month)))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return options


@app.route("/doors")
def doors():
    today = datetime.date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
    except ValueError:
        year, month = today.year, today.month

    error = None
    locks = []
    if not KWIKSET_EMAIL and not db_load_kwikset_auth():
        error = ("Kwikset isn't connected yet. Run auth-setup.js from the "
                  "kwikset-mcp-node project once, then set KWIKSET_EMAIL and "
                  "KWIKSET_REFRESH_TOKEN.")
    else:
        try:
            client = get_kwikset_client()
            locks = client.list_locks()
        except Exception as e:
            error = str(e)

    selected_device_id = request.args.get("device_id") or (locks[0]["device_id"] if locks else None)

    guests = []
    guest_error = None
    if not error:
        try:
            guests = fetch_bookings_for_month(year, month)
        except Exception as e:
            guest_error = str(e)

    lock_options = "".join(
        f'<option value="{lk["device_id"]}" {"selected" if lk["device_id"] == selected_device_id else ""}>'
        f'{lk["name"]} ({lk["home"]})</option>'
        for lk in locks
    )
    month_options = "".join(
        f'<option value="{y}-{m:02d}" {"selected" if sel else ""}>{label}</option>'
        for y, m, label, sel in _month_options(year, month)
    )

    if guest_error:
        guest_rows = f'<p class="empty-state">Couldn\'t load bookings: {guest_error}</p>'
    elif not guests:
        guest_rows = '<p class="empty-state">No guests arriving this month.</p>'
    else:
        rows = []
        for g in guests:
            phone = fetch_guest_phone(g["guest_id"]) if g.get("guest_id") else None
            last4 = phone_last4(phone) or "—"
            existing = (
                db_find_access_code_for_booking(selected_device_id, g["booking_key"])
                if selected_device_id else None
            )
            if existing:
                status_label = f"Sent (slot {existing['slot']}, code {existing['code']})"
                status_class = "status-on"
            else:
                status_label = "Not sent"
                status_class = "status-off"

            rows.append(DOORS_ROW_TEMPLATE.format(
                first_name=g["first_name"],
                last_name=g["last_name"],
                arrival_str=format_date(g["arrival"]),
                departure_str=format_date(g["departure"]),
                last4=last4,
                status_label=status_label,
                status_class=status_class,
                booking_key=g["booking_key"],
                selected_device_id_for_row=selected_device_id or "",
                year_for_row=year,
                month_for_row=month,
                send_disabled="disabled" if (existing or not selected_device_id or last4 == "—") else "",
                send_label="Already sent" if existing else "Send code",
            ))
        guest_rows = "".join(rows)

    html = DOORS_TEMPLATE.format(
        error_banner=f'<div class="error-banner">{error}</div>' if error else "",
        lock_options=lock_options or '<option value="">No locks found</option>',
        month_options=month_options,
        guest_rows=guest_rows,
        selected_device_id=selected_device_id or "",
        selected_month_value=f"{year}-{month:02d}",
    )
    return _no_cache(Response(html, mimetype="text/html"))


@app.route("/doors/send", methods=["POST"])
def doors_send():
    device_id = request.form.get("device_id")
    booking_key = request.form.get("booking_key")
    year = request.form.get("year")
    month = request.form.get("month")
    if not device_id or not booking_key:
        return "Missing device_id or booking_key", 400
    try:
        year, month = int(year), int(month)
    except (TypeError, ValueError):
        return "Missing or invalid year/month", 400

    try:
        guests = fetch_bookings_for_month(year, month)
        guest = next((g for g in guests if g["booking_key"] == booking_key), None)
        if guest is None:
            return "Guest not found for that month -- try reloading the page", 404
        send_door_code_for_guest(device_id, guest)
    except Exception as e:
        return f"Failed to send door code: {e}", 500

    return redirect(f"/doors?device_id={device_id}&year={year}&month={month}")


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
  <a class="back-link" href="/">&larr; Back to welcome screen</a> · <a class="back-link" href="/cleaning">Cleaning checklist</a> · <a class="back-link" href="/pool">Pool control</a> · <a class="back-link" href="/doors">Door codes &rarr;</a>
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
  <a class="back-link" href="/">&larr; Back to welcome screen</a> · <a class="back-link" href="/manage">Manage arrivals</a> · <a class="back-link" href="/pool">Pool control</a> · <a class="back-link" href="/doors">Door codes &rarr;</a>
  <h1>Cleaning Checklist</h1>
  <p class="subtitle">Last refreshed: {last_updated} · <a class="back-link" href="/refresh">Refresh now</a></p>
  {error_banner}

  {cards}

  <p class="footer-note">Checklists are per booking and reset automatically once a booking is no longer upcoming. Checking a box saves immediately — no need to submit anything. This page has no login — don't share the URL publicly.</p>
</div>
</body>
</html>
"""


POOL_SENSOR_CARD_TEMPLATE = """
    <div class="sensor-card">
      <div class="sensor-label">{label}</div>
      <div class="sensor-value">{value}°</div>
    </div>
"""

POOL_SETPOINT_CARD_TEMPLATE = """
  <div class="setpoint-card">
    <div class="setpoint-header">
      <div class="setpoint-label">{label}</div>
      <span class="status-badge {status_class}">{status_label}</span>
    </div>
    <form method="POST" action="/pool/set_temperature" class="setpoint-form">
      <input type="hidden" name="device_key" value="{key}">
      <input type="number" name="temperature" value="{value}" class="temp-input">
      <button type="submit" class="temp-set-btn">Set</button>
    </form>
  </div>
"""

POOL_EQUIPMENT_CARD_TEMPLATE = """
  <div class="equipment-card {card_class}">
    <div class="equipment-info">
      <div class="equipment-label">{label}</div>
      <span class="status-badge {status_class}">{status_label}</span>
    </div>
    <form method="POST" action="/pool/toggle">
      <input type="hidden" name="device_key" value="{key}">
      <button type="submit" class="toggle-btn">{button_label}</button>
    </form>
  </div>
"""

POOL_SCHEDULE_ROW_TEMPLATE = """
  <div class="schedule-row {row_class}">
    <div class="schedule-info">
      <div class="schedule-device">{device_label}</div>
      <div class="schedule-times">{on_time} &rarr; {off_time} · {days_label}</div>
    </div>
    <span class="status-badge {status_class}">{status_label}</span>
    <form method="POST" action="/pool/schedule/toggle" class="schedule-btn-form">
      <input type="hidden" name="schedule_id" value="{schedule_id}">
      <button type="submit" class="toggle-btn">{toggle_label}</button>
    </form>
    <form method="POST" action="/pool/schedule/delete" class="schedule-btn-form">
      <input type="hidden" name="schedule_id" value="{schedule_id}">
      <button type="submit" class="delete-btn">Delete</button>
    </form>
  </div>
"""

POOL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Pool Control — {system_name}</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,500;9..144,600&family=Work+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{{
    --creek: #1F3F3D; --creek-deep: #142B29; --limestone: #EFEAD9;
    --sage: #7C8B65; --clay: #C1652F; --bark: #2A2018;
  }}
  *{{box-sizing:border-box;}}
  body{{ margin:0; background: var(--limestone); color: var(--bark);
    font-family:'Work Sans', sans-serif; padding: 20px 20px 30px; font-size: 14px; }}
  .wrap{{ max-width: 900px; margin:0 auto; }}
  h1{{ font-family:'Fraunces', serif; font-weight:500; font-size: 24px;
    color: var(--creek-deep); margin: 8px 0 4px; display:inline-block; }}
  .subtitle{{ color:#77705C; margin: 0 0 16px; font-size:12px; }}
  .back-link{{ font-size: 12px; color: var(--creek); text-decoration:none; }}
  .back-link:hover{{ text-decoration:underline; }}
  .online-badge{{
    display:inline-block; font-size: 11px; font-weight:600; padding: 2px 8px;
    border-radius: 100px; margin-left: 10px; vertical-align: middle;
  }}
  .online-yes{{ background: var(--sage); color:#fff; }}
  .online-no{{ background:#C1652F; color:#fff; }}
  .error-banner{{
    background:#FBEAE0; border:1px solid #E8B99B; color:#8A3D14;
    padding:10px 14px; border-radius:10px; margin-bottom:14px; font-size:13px;
  }}
  .empty-state{{ text-align:center; color:#9A9276; padding: 14px; font-size: 13px; }}
  .section-title{{
    font-family:'Fraunces', serif; font-weight:500; font-size: 15px;
    color: var(--creek-deep); margin: 18px 0 8px;
  }}
  .sensor-grid{{ display:flex; gap:10px; flex-wrap:wrap; }}
  .sensor-card{{
    background:#fff; border:1px solid #E2DBC5; border-radius:12px;
    padding: 10px 16px; min-width: 90px; text-align:center;
  }}
  .sensor-label{{ font-size: 10px; text-transform:uppercase; letter-spacing:0.06em; color:#8A7F63; margin-bottom:3px; }}
  .sensor-value{{ font-family:'Fraunces', serif; font-weight:500; font-size: 20px; color: var(--creek-deep); }}
  .setpoint-grid{{ display:grid; grid-template-columns: 1fr 1fr; gap:10px; }}
  .setpoint-card{{ background:#fff; border:1px solid #E2DBC5; border-radius:12px; padding: 10px 12px; }}
  .setpoint-header{{ display:flex; justify-content:space-between; align-items:center; margin-bottom: 8px; }}
  .setpoint-label{{ font-family:'Fraunces', serif; font-weight:500; font-size: 13px; color: var(--creek-deep); }}
  .setpoint-form{{ display:flex; gap:6px; margin:0; }}
  .temp-input{{
    width: 56px; font-family:'Work Sans', sans-serif; font-size: 13px;
    padding: 5px 6px; border-radius: 7px; border:1px solid #DCD4B8;
  }}
  .temp-set-btn{{
    font-family:'Work Sans', sans-serif; font-size: 11px; font-weight:500;
    padding: 5px 12px; border-radius: 100px; border:1px solid var(--creek);
    background: var(--creek); color:#fff; cursor:pointer;
  }}
  .temp-set-btn:hover{{ background: var(--creek-deep); }}
  .equipment-grid{{ display:grid; grid-template-columns: repeat(3, 1fr); gap:8px; }}
  .equipment-card{{
    background:#fff; border:1px solid #E2DBC5; border-radius:12px;
    padding: 9px 11px; display:flex; justify-content:space-between; align-items:center; gap:8px;
  }}
  .equipment-card-on{{ border-color: var(--sage); background:#F7F9F2; }}
  .equipment-label{{ font-size: 12px; font-weight:500; color: var(--bark); margin-bottom: 3px; }}
  .status-badge{{
    display:inline-block; font-size: 10px; font-weight:600; padding: 1px 7px;
    border-radius: 100px;
  }}
  .status-on{{ background: var(--sage); color:#fff; }}
  .status-off{{ background:#DCD4B8; color:#5C5443; }}
  .toggle-btn{{
    font-family:'Work Sans', sans-serif; font-size: 11px; font-weight:500;
    padding: 5px 10px; border-radius: 100px; border:1px solid #DCD4B8;
    background:#fff; color: var(--bark); cursor:pointer; white-space:nowrap;
  }}
  .toggle-btn:hover{{ border-color: var(--clay); color: var(--clay); }}
  .footer-note{{ margin-top: 18px; font-size: 11px; color:#9A9276; }}
  .schedule-row{{
    background:#fff; border:1px solid #E2DBC5; border-radius:12px;
    padding: 9px 12px; display:flex; align-items:center; gap:10px; margin-bottom:8px;
  }}
  .schedule-row-disabled{{ opacity: 0.55; }}
  .schedule-info{{ flex:1; min-width:0; }}
  .schedule-device{{ font-size: 13px; font-weight:500; color: var(--bark); }}
  .schedule-times{{ font-size: 11px; color:#77705C; margin-top:2px; }}
  .schedule-btn-form{{ margin:0; }}
  .delete-btn{{
    font-family:'Work Sans', sans-serif; font-size: 11px; font-weight:500;
    padding: 5px 10px; border-radius: 100px; border:1px solid #E8B99B;
    background:#fff; color:#8A3D14; cursor:pointer; white-space:nowrap;
  }}
  .delete-btn:hover{{ background:#FBEAE0; }}
  .add-schedule-card{{
    background:#fff; border:1px solid #E2DBC5; border-radius:12px;
    padding: 14px; margin-top: 10px;
  }}
  .add-schedule-form{{ display:flex; flex-wrap:wrap; gap:10px; align-items:flex-end; }}
  .form-field{{ display:flex; flex-direction:column; gap:4px; }}
  .form-field label{{ font-size: 10px; text-transform:uppercase; letter-spacing:0.06em; color:#8A7F63; }}
  .form-field select, .form-field input[type=time]{{
    font-family:'Work Sans', sans-serif; font-size: 13px;
    padding: 6px 8px; border-radius: 7px; border:1px solid #DCD4B8;
  }}
  .days-picker{{ display:flex; gap:4px; }}
  .day-chip{{ position:relative; }}
  .day-chip input{{ position:absolute; opacity:0; width:100%; height:100%; margin:0; cursor:pointer; }}
  .day-chip span{{
    display:inline-flex; align-items:center; justify-content:center;
    width: 30px; height: 30px; border-radius: 8px; border:1px solid #DCD4B8;
    font-size: 11px; color:#77705C; cursor:pointer;
  }}
  .day-chip input:checked + span{{ background: var(--creek); border-color: var(--creek); color:#fff; }}
  .add-schedule-btn{{
    font-family:'Work Sans', sans-serif; font-size: 12px; font-weight:500;
    padding: 7px 16px; border-radius: 100px; border:1px solid var(--creek);
    background: var(--creek); color:#fff; cursor:pointer;
  }}
  .add-schedule-btn:hover{{ background: var(--creek-deep); }}
  @media (max-width: 700px){{
    .equipment-grid{{ grid-template-columns: repeat(2, 1fr); }}
  }}
  @media (max-width: 480px){{
    .setpoint-grid, .equipment-grid{{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back-link" href="/">&larr; Back to welcome screen</a> · <a class="back-link" href="/manage">Manage arrivals</a> · <a class="back-link" href="/cleaning">Cleaning checklist</a> · <a class="back-link" href="/doors">Door codes &rarr;</a>
  <div><h1>{system_name}</h1>{online_badge}</div>
  <p class="subtitle"><a class="back-link" href="/pool">Refresh now</a></p>
  {error_banner}

  <h2 class="section-title">Readings</h2>
  <div class="sensor-grid">
    {sensor_cards}
  </div>

  <h2 class="section-title">Temperature</h2>
  <div class="setpoint-grid">
    {setpoint_cards}
  </div>

  <h2 class="section-title">Equipment</h2>
  <div class="equipment-grid">
    {equipment_cards}
  </div>

  <h2 class="section-title" id="schedule">Schedules</h2>
  <div>
    {schedule_rows}
  </div>
  <div class="add-schedule-card">
    <form method="POST" action="/pool/schedule/add" class="add-schedule-form"
          onsubmit="document.getElementById('device_label_hidden').value = document.getElementById('device_key_select').selectedOptions[0].dataset.label || '';">
      <div class="form-field">
        <label>Device</label>
        <select name="device_key" id="device_key_select" required>
          {device_options}
        </select>
        <input type="hidden" name="device_label" id="device_label_hidden">
      </div>
      <div class="form-field">
        <label>Turn on</label>
        <input type="time" name="on_time" value="08:00" required>
      </div>
      <div class="form-field">
        <label>Turn off</label>
        <input type="time" name="off_time" value="18:00" required>
      </div>
      <div class="form-field">
        <label>Days</label>
        <div class="days-picker">
          <label class="day-chip"><input type="checkbox" name="days" value="0" checked><span>M</span></label>
          <label class="day-chip"><input type="checkbox" name="days" value="1" checked><span>T</span></label>
          <label class="day-chip"><input type="checkbox" name="days" value="2" checked><span>W</span></label>
          <label class="day-chip"><input type="checkbox" name="days" value="3" checked><span>T</span></label>
          <label class="day-chip"><input type="checkbox" name="days" value="4" checked><span>F</span></label>
          <label class="day-chip"><input type="checkbox" name="days" value="5" checked><span>S</span></label>
          <label class="day-chip"><input type="checkbox" name="days" value="6" checked><span>S</span></label>
        </div>
      </div>
      <button type="submit" class="add-schedule-btn">Add schedule</button>
    </form>
  </div>

  <p class="footer-note">Pool state is fetched live on every visit to this page — it isn't cached. Schedules run in the background continuously, whether or not this page is open, and are saved to disk so they survive restarts and deploys. This page has no login — don't share the URL publicly.</p>
</div>
</body>
</html>
"""


DOORS_ROW_TEMPLATE = """
      <tr>
        <td class="guest-name">{first_name} {last_name}</td>
        <td>{arrival_str}</td>
        <td>{departure_str}</td>
        <td>{last4}</td>
        <td><span class="status-badge {status_class}">{status_label}</span></td>
        <td>
          <form method="POST" action="/doors/send">
            <input type="hidden" name="device_id" value="{selected_device_id_for_row}">
            <input type="hidden" name="booking_key" value="{booking_key}">
            <input type="hidden" name="year" value="{year_for_row}">
            <input type="hidden" name="month" value="{month_for_row}">
            <button type="submit" class="select-btn" {send_disabled}>{send_label}</button>
          </form>
        </td>
      </tr>
"""

DOORS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Door Codes — Villa Brushy Creek</title>
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
  .subtitle{{ color:#77705C; margin: 0 0 20px; font-size:14px; }}
  .back-link{{ font-size: 13px; color: var(--creek); text-decoration:none; }}
  .back-link:hover{{ text-decoration:underline; }}
  .error-banner{{
    background:#FBEAE0; border:1px solid #E8B99B; color:#8A3D14;
    padding:12px 16px; border-radius:10px; margin-bottom:20px; font-size:14px;
  }}
  .empty-state{{ text-align:center; color:#9A9276; padding: 30px; }}
  .controls{{ display:flex; gap:14px; margin: 20px 0 24px; flex-wrap:wrap; }}
  .controls form{{ margin:0; }}
  .controls select{{
    font-family:'Work Sans', sans-serif; font-size: 14px;
    padding: 9px 12px; border-radius: 9px; border:1px solid #DCD4B8; background:#fff;
  }}
  table{{ width:100%; border-collapse: collapse; background:#fff;
    border-radius: 14px; overflow:hidden; border:1px solid #E2DBC5; }}
  th{{
    text-align:left; font-size: 11px; text-transform:uppercase; letter-spacing:0.08em;
    color:#8A7F63; padding: 14px 16px; border-bottom: 1px solid #E2DBC5; background:#FAF6E9;
  }}
  td{{ padding: 14px 16px; border-bottom: 1px solid #EFEAD9; font-size: 14px; vertical-align: middle; }}
  tr:last-child td{{ border-bottom:none; }}
  .guest-name{{ font-family:'Fraunces', serif; font-weight:500; font-size: 16px; color: var(--creek-deep); }}
  .status-badge{{
    display:inline-block; font-size: 11px; font-weight:600; padding: 2px 9px;
    border-radius: 100px;
  }}
  .status-on{{ background: var(--sage); color:#fff; }}
  .status-off{{ background:#DCD4B8; color:#5C5443; }}
  .select-btn{{
    font-family:'Work Sans', sans-serif; font-size: 13px; font-weight:500;
    padding: 8px 14px; border-radius: 100px; border:1px solid var(--clay);
    background:#fff; color: var(--clay); cursor:pointer; white-space:nowrap;
  }}
  .select-btn:hover{{ background: var(--clay); color:#fff; }}
  .select-btn[disabled]{{
    border-color:#DCD4B8; color:#9A9276; cursor:default; background:#F3F0DD;
  }}
  .footer-note{{ margin-top: 24px; font-size: 12px; color:#9A9276; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back-link" href="/">&larr; Back to welcome screen</a> · <a class="back-link" href="/manage">Manage arrivals</a> · <a class="back-link" href="/cleaning">Cleaning checklist</a> · <a class="back-link" href="/pool">Pool control</a> · <a class="back-link" href="/doors">Door codes &rarr;</a>
  <h1>Door Codes</h1>
  <p class="subtitle">Sends a code (last 4 of the guest's phone) valid only for their stay dates.</p>
  {error_banner}

  <form method="GET" action="/doors" class="controls">
    <select name="device_id" onchange="this.form.submit()">
      {lock_options}
    </select>
    <select name="month_select" onchange="
      var v=this.value.split('-'); var f=this.form;
      var y=document.createElement('input'); y.type='hidden'; y.name='year'; y.value=v[0]; f.appendChild(y);
      var m=document.createElement('input'); m.type='hidden'; m.name='month'; m.value=v[1]; f.appendChild(m);
      f.submit();">
      {month_options}
    </select>
  </form>

  <table>
    <thead>
      <tr>
        <th>Guest</th><th>Arrival</th><th>Departure</th><th>Phone (last 4)</th><th>Status</th><th></th>
      </tr>
    </thead>
    <tbody>
      {guest_rows}
    </tbody>
  </table>

  <p class="footer-note">"Sent" only reflects codes this app itself has created -- Kwikset's API has no way to read codes back off the physical lock, so this can't detect codes added via the Kwikset app or keypad. This page has no login — don't share the URL publicly, since it can create real door access codes.</p>
</div>
</body>
</html>
"""


# Load persisted state from disk before anything else starts, so the
# background threads (which run immediately) see correct data from the
# first tick rather than starting from a blank slate every deploy.
try:
    init_db()
    _loaded = db_load_all()
    with _cache_lock:
        _cache["pool_schedules"] = _loaded["pool_schedules"]
        _cache["cleaning"] = _loaded["cleaning"]
        _cache["mode"] = _loaded["mode"]
        _cache["selected_key"] = _loaded["selected_key"]
    print(f"[{datetime.datetime.now()}] Loaded from database: "
          f"{len(_loaded['pool_schedules'])} pool schedule(s), "
          f"{len(_loaded['cleaning'])} cleaning record(s), "
          f"mode={_loaded['mode']}")
except Exception as e:
    # If the database can't be reached/initialized for any reason, the
    # app still starts and works exactly like before this feature
    # existed (in-memory only) -- it just won't persist until the
    # underlying issue (e.g. a missing/misconfigured disk) is fixed.
    print(f"[{datetime.datetime.now()}] Database init failed, continuing "
          f"with in-memory-only state: {e}", file=sys.stderr)

# Start the background refresh loop as soon as the module is imported.
# This runs whether the app is launched via `python app.py` (dev) or
# via gunicorn (Render/production), since gunicorn imports this module
# and never executes the `if __name__ == "__main__"` block below.
threading.Thread(target=background_loop, daemon=True).start()
threading.Thread(target=pool_scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    # Local dev only. On Render, gunicorn runs this instead (see
    # requirements.txt / start command in the deploy notes above) --
    # Flask's built-in server here isn't meant for production traffic.
    print(f"Starting server on http://0.0.0.0:{PORT}  (Ctrl+C to stop)")
    app.run(host="0.0.0.0", port=PORT)
