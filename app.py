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
import html
import base64
import uuid
import json
import sqlite3
import secrets
import functools
import asyncio
import threading
import time
import datetime
from zoneinfo import ZoneInfo
import requests
from flask import Flask, Response, request, redirect, session, render_template
from markupsafe import Markup
from werkzeug.security import generate_password_hash, check_password_hash

import kwikset_client



def frag(template_name, **context):
    """Render a partial and mark the result safe for embedding in a parent.

    Jinja autoescapes by default, so a plain str returned by
    render_template() would be escaped into visible angle brackets when
    interpolated into another template. Partials are trusted markup that
    this app just rendered, so they are wrapped in Markup; everything
    else stays escaped, which is what makes the guest-data placeholders
    safe without a manual h() call at each one.
    """
    return Markup(render_template(template_name, **context))


def html_join(parts):
    """Join fragments this app rendered into a single value safe to embed.

    Markup("").join() escapes any plain str it is given, which is the
    correct default -- but every caller here passes markup it just built
    itself (partials from frag(), or <option> tags from a literal
    f-string), so the parts are wrapped as trusted.

    That trust is why guest and device data interpolated into those
    f-strings must be escaped with h() at the point it goes in. It is not
    escaped here.
    """
    return Markup("").join(Markup(part) for part in parts)



def h(value):
    """Escape untrusted text before it is interpolated into HTML.

    Every page in this app is a plain str.format() string, and format()
    does no escaping of its own. Anything originating outside this app --
    guest names and message bodies from OwnerRez, and API error text,
    which get_ownerrez errors deliberately include verbatim -- must go
    through here first, or a guest can inject markup that runs in the
    logged-in admin's browser.

    Returns "" for None so templates render a blank rather than "None".
    """
    return html.escape("" if value is None else str(value), quote=True)


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
# Slots below this are left alone -- low slot numbers are the ones most
# likely to already be occupied by codes set manually through the
# Kwikset app or keypad (which this app can't see -- see the /doors
# README section on why "sent" tracking is local-only).
KWIKSET_START_SLOT = int(os.environ.get("KWIKSET_START_SLOT", "5"))

# Auth. SECRET_KEY signs the session cookie -- without setting this env
# var, a random key is generated at every process start, which means
# everyone gets logged out on every restart/redeploy. Set it explicitly
# in Render for persistent sessions (any long random string works, e.g.
# `python3 -c "import secrets; print(secrets.token_hex(32))"`).
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")

# Guest messaging. OwnerRez has no endpoint to list open/unread messages --
# by their own design, the only way to learn about a new guest message is a
# webhook they push the moment one arrives. So /webhooks/ownerrez receives
# and stores those events ourselves; /messages reads from that local store.
# OWNERREZ_WEBHOOK_SECRET is optional but recommended -- without it, anyone
# who finds the webhook URL could post fake messages into your inbox.
OWNERREZ_WEBHOOK_SECRET = os.environ.get("OWNERREZ_WEBHOOK_SECRET")
# Used to build the callback URL when registering the webhook subscription
# with OwnerRez -- set this to your real Render URL, e.g.
# https://villa-brushy-creek-welcome.onrender.com
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # optional -- AI drafts

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
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS message_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            received_utc TEXT NOT NULL,
            category     TEXT,
            action       TEXT,
            thread_id    TEXT,
            booking_id   TEXT,
            guest        TEXT,
            body         TEXT,
            is_incoming  INTEGER,
            handled      INTEGER NOT NULL DEFAULT 0,
            raw          TEXT,
            draft_reply  TEXT,
            sent_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_msg_open ON message_events (handled, category);
    """)
    db.commit()

    # Seed the bootstrap admin account with NO password set -- the app's
    # before_request guard sends every visitor to /setup until someone
    # sets this account's first password there. Never seed a real
    # password here, and never accept one via chat/env var for this --
    # it must be set by whoever can actually reach the live site.
    existing_admin = db.execute(
        "SELECT 1 FROM users WHERE username = ?", (ADMIN_USERNAME,)
    ).fetchone()
    if not existing_admin:
        db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, NULL, ?)",
            (ADMIN_USERNAME, datetime.datetime.now().isoformat()),
        )
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
    """Returns the next free slot for this device, starting from
    KWIKSET_START_SLOT (not 1) -- see its definition for why."""
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(MAX(slot), ?) + 1 AS next_slot FROM kwikset_access_codes WHERE device_id = ?",
        (KWIKSET_START_SLOT - 1, device_id),
    ).fetchone()
    return row["next_slot"]


def db_find_access_code_for_booking(device_id, booking_key):
    db = get_db()
    return db.execute(
        "SELECT * FROM kwikset_access_codes WHERE device_id = ? AND booking_key = ?",
        (device_id, booking_key),
    ).fetchone()


def db_list_all_access_codes():
    db = get_db()
    return db.execute(
        "SELECT * FROM kwikset_access_codes ORDER BY created_at DESC"
    ).fetchall()


def db_delete_access_code_record(device_id, slot):
    db = get_db()
    db.execute(
        "DELETE FROM kwikset_access_codes WHERE device_id = ? AND slot = ?",
        (device_id, slot),
    )
    db.commit()


def db_list_users():
    db = get_db()
    return db.execute(
        "SELECT id, username, password_hash, created_at, updated_at FROM users ORDER BY id"
    ).fetchall()


def db_get_user_by_username(username):
    db = get_db()
    return db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def db_get_user_by_id(user_id):
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def db_any_user_has_password():
    """False means no one can log in yet -- the app should be showing
    the /setup bootstrap flow instead of the normal login page."""
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM users WHERE password_hash IS NOT NULL AND password_hash != '' LIMIT 1"
    ).fetchone()
    return row is not None


def db_create_user(username, password):
    db = get_db()
    now = datetime.datetime.now().isoformat()
    db.execute(
        "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (username, generate_password_hash(password), now, now),
    )
    db.commit()


def db_set_user_password(user_id, password):
    db = get_db()
    db.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (generate_password_hash(password), datetime.datetime.now().isoformat(), user_id),
    )
    db.commit()


def db_delete_user(user_id):
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()


# ---------------------------------------------------------------------------
# GUEST MESSAGING -- webhook-driven inbox
# ---------------------------------------------------------------------------
# parse_message_event is a direct port of the same function from the
# author's own ownerrez-mcp-node project (pasted directly, not guessed) --
# its own docstring is honest that OwnerRez's payload shape isn't 100%
# pinned down, so this uses the same defensive multi-key fallback approach
# rather than assuming a single exact schema.

_VALIDATION_KEYS = ("validationToken", "validation_token", "challenge", "validation")


def _first(obj, keys):
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return None


def _extract_validation(mapping):
    if not isinstance(mapping, dict):
        return None
    for k in _VALIDATION_KEYS:
        if mapping.get(k):
            return str(mapping[k])
    return None


def parse_message_event(payload):
    """Best-effort extraction of a message event from an OwnerRez webhook
    body. Field names vary across payload shapes -- fallbacks are used
    everywhere, and the raw payload is always kept so nothing is lost."""
    entity = payload.get("entity") or payload.get("resource") or payload.get("data") or payload

    category = _first(payload, ["category", "type", "resource_type", "event_type"]) or "message"
    action = _first(payload, ["action", "event", "operation"])
    thread_id = _first(entity, ["threadId", "thread_id", "conversation_id", "conversationId"])
    booking_id = _first(entity, ["booking_id", "bookingId", "booking"])
    body = _first(entity, ["body", "message", "text", "content"])

    guest = None
    guest_obj = entity.get("guest") if isinstance(entity, dict) else None
    if isinstance(guest_obj, dict):
        guest = (
            " ".join(x for x in [guest_obj.get("first_name"), guest_obj.get("last_name")] if x).strip()
            or guest_obj.get("name")
        )
    guest = guest or _first(entity, ["guest_name", "from", "sender"])

    is_incoming = _first(entity, ["is_incoming", "incoming", "from_guest", "inbound"])
    direction = _first(entity, ["direction"])
    if is_incoming is None and isinstance(direction, str):
        is_incoming = direction.lower() in ("in", "inbound", "incoming", "from_guest")

    return {
        "category": str(category).lower() if category else None,
        "action": action,
        "thread_id": thread_id,
        "booking_id": booking_id,
        "guest": guest,
        "body": body,
        "is_incoming": is_incoming,
        "raw": payload,
    }


def db_add_message_event(event):
    db = get_db()
    cur = db.execute(
        """INSERT INTO message_events
           (received_utc, category, action, thread_id, booking_id, guest,
            body, is_incoming, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            event.get("category"),
            event.get("action"),
            str(event["thread_id"]) if event.get("thread_id") is not None else None,
            str(event["booking_id"]) if event.get("booking_id") is not None else None,
            event.get("guest"),
            event.get("body"),
            (1 if event.get("is_incoming") else 0) if event.get("is_incoming") is not None else None,
            json.dumps(event.get("raw"), default=str) if event.get("raw") is not None else None,
        ),
    )
    db.commit()
    return cur.lastrowid


def db_list_open_messages(limit=100):
    """Unhandled message events (inbound or unknown-direction), newest first."""
    db = get_db()
    return db.execute(
        """SELECT * FROM message_events
           WHERE handled = 0
             AND (category = 'message' OR category IS NULL)
             AND (is_incoming = 1 OR is_incoming IS NULL)
           ORDER BY id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def db_get_message_event(event_id):
    db = get_db()
    return db.execute("SELECT * FROM message_events WHERE id = ?", (event_id,)).fetchone()


def db_mark_message_handled(event_id, handled=True, sent=False):
    db = get_db()
    if sent:
        db.execute(
            "UPDATE message_events SET handled = ?, sent_at = ? WHERE id = ?",
            (1 if handled else 0, datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), event_id),
        )
    else:
        db.execute("UPDATE message_events SET handled = ? WHERE id = ?", (1 if handled else 0, event_id))
    db.commit()


def db_save_message_draft(event_id, draft_text):
    db = get_db()
    db.execute("UPDATE message_events SET draft_reply = ? WHERE id = ?", (draft_text, event_id))
    db.commit()


def verify_login(username, password):
    """Returns the user row on success, None on any failure. Deliberately
    takes the same shape of time whether the username exists or not (by
    always calling check_password_hash against *something*) so a failed
    login can't be used to enumerate valid usernames by timing."""
    user = db_get_user_by_username(username)
    if user and user["password_hash"]:
        if check_password_hash(user["password_hash"], password):
            return user
        return None
    # Username doesn't exist, or has no password set yet -- still run a
    # hash check against a dummy value so this branch takes comparable
    # time to the real check above.
    check_password_hash(generate_password_hash("dummy"), password)
    return None


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
    # Confirmed via OwnerRez's own webhook payload docs + a staff forum
    # reply: agreements come back as a list of {date, name} -- an entry
    # with a real date means it was actually signed. Unsigned/never-sent
    # agreements simply aren't in the list (or would have a null date) --
    # handled defensively either way.
    agreements = b.get("agreements") or []
    agreement_signed = any(a.get("date") for a in agreements)
    agreement_signed_date = next((a.get("date") for a in agreements if a.get("date")), None)
    return {
        "booking_key": _booking_key(b),
        "booking_id": b.get("id"),
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
        "agreement_signed": agreement_signed,
        "agreement_signed_date": agreement_signed_date,
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
            "include_agreements": "true",
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


_DEPOSIT_DESCRIPTION_MARKER = "security deposit"


def fetch_deposit_status(booking_id):
    """Checks whether a security deposit payment exists for this booking.
    OwnerRez has no distinct payment `type` for this (confirmed against
    real account data -- deposit payments come back as type="credit_card",
    same as regular payments) -- the only reliable signal is "security
    deposit" appearing in the payment description. Returns a dict with
    the match, or None if the lookup itself failed (best-effort, like
    the phone lookup -- one guest's failure shouldn't break the page)."""
    if not OWNERREZ_USERNAME or not OWNERREZ_TOKEN:
        return None
    headers = {
        "User-Agent": "Villa Brushy Creek Welcome Screen/1.0",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(
            f"{API_BASE}/payments",
            params={"booking_id": booking_id},
            auth=(OWNERREZ_USERNAME, OWNERREZ_TOKEN),
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"[{datetime.datetime.now()}] Deposit status lookup failed for "
              f"booking_id={booking_id}: {e}", file=sys.stderr)
        return None

    payments = payload.get("items") or payload.get("payments") or []
    for p in payments:
        description = (p.get("description") or "").lower()
        if _DEPOSIT_DESCRIPTION_MARKER in description:
            return {"received": True, "amount": p.get("amount"), "description": p.get("description")}
    return {"received": False, "amount": None, "description": None}


def _ownerrez_headers():
    return {
        "User-Agent": "Villa Brushy Creek Welcome Screen/1.0",
        "Accept": "application/json",
    }


def _raise_with_body(resp):
    """requests' default raise_for_status() only gives you the status
    code and a generic phrase -- the actual reason (validation error,
    missing consent, etc.) is almost always in the response body, and
    every debugging session in this project so far has needed that body
    to actually find the real cause. Surface it directly instead of
    hiding it behind a second manual lookup."""
    if resp.ok:
        return
    body_text = ""
    try:
        body_text = resp.text[:800]
    except Exception:
        pass
    raise RuntimeError(
        f"{resp.status_code} {resp.reason} for {resp.request.method} {resp.request.url}"
        + (f": {body_text}" if body_text else "")
    )


def ownerrez_create_webhook_subscription(url, category):
    """POST /v2/webhooksubscriptions -- confirmed directly against the
    author's own working ownerrez-mcp-node implementation."""
    if not OWNERREZ_USERNAME or not OWNERREZ_TOKEN:
        raise RuntimeError("OwnerRez credentials aren't configured.")
    resp = requests.post(
        f"{API_BASE}/webhooksubscriptions",
        json={"WebhookUrl": url, "category": category},
        auth=(OWNERREZ_USERNAME, OWNERREZ_TOKEN),
        headers=_ownerrez_headers(),
        timeout=15,
    )
    _raise_with_body(resp)
    return resp.json()


def ownerrez_list_webhook_subscriptions():
    if not OWNERREZ_USERNAME or not OWNERREZ_TOKEN:
        raise RuntimeError("OwnerRez credentials aren't configured.")
    resp = requests.get(
        f"{API_BASE}/webhooksubscriptions",
        auth=(OWNERREZ_USERNAME, OWNERREZ_TOKEN),
        headers=_ownerrez_headers(),
        timeout=15,
    )
    _raise_with_body(resp)
    payload = resp.json()
    return payload.get("items") or payload.get("webhooksubscriptions") or payload.get("subscriptions") or []


def ownerrez_send_message(thread_id, body):
    """POST /v2/messages -- confirmed directly against the author's own
    working ownerrez-mcp-node send_message implementation (threadId +
    body, no other required fields)."""
    if not OWNERREZ_USERNAME or not OWNERREZ_TOKEN:
        raise RuntimeError("OwnerRez credentials aren't configured.")
    resp = requests.post(
        f"{API_BASE}/messages",
        json={"threadId": int(thread_id), "body": body},
        auth=(OWNERREZ_USERNAME, OWNERREZ_TOKEN),
        headers=_ownerrez_headers(),
        timeout=15,
    )
    _raise_with_body(resp)
    return resp.json()


def generate_ai_draft(event):
    """Calls Anthropic's Messages API directly (same pattern as every
    other integration in this app: raw `requests`, no SDK dependency).
    Grounds the draft only in the guest's message and, if resolvable, the
    real booking dates/property -- never invents specifics like wifi
    passwords or door codes, leaving a bracketed placeholder instead,
    matching the same philosophy as this project's original guest-reply
    design doc."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "AI drafting isn't configured yet. Set ANTHROPIC_API_KEY to enable "
            "AI-suggested replies -- until then, replies can still be typed and "
            "sent manually."
        )

    booking_context = ""
    booking_id = event["booking_id"] if "booking_id" in event.keys() else None
    if booking_id:
        with _cache_lock:
            match = next(
                (g for g in _cache.get("upcoming", []) if str(g.get("booking_id")) == str(booking_id)),
                None,
            )
        if match:
            booking_context = (
                f"Their booking: arriving {match['arrival']}, departing {match['departure']}, "
                f"at {match['property_name']}.\n"
            )

    guest_name = event["guest"] if "guest" in event.keys() and event["guest"] else "the guest"
    incoming_body = event["body"] if "body" in event.keys() and event["body"] else "(no message text)"

    prompt = (
        f"You are helping a vacation rental host draft a reply to a guest message "
        f"for {PROPERTY_DISPLAY_NAME}.\n\n"
        f"Guest: {guest_name}\n"
        f"{booking_context}"
        f'Guest\'s message: "{incoming_body}"\n\n'
        "Write a warm, concise, professional reply (under 100 words). Ground it "
        "only in the information given above -- never invent specifics like wifi "
        "passwords, door codes, or directions you don't actually have. If the "
        "guest is asking for something not provided here, leave a bracketed "
        "placeholder like [confirm wifi password] for the host to fill in before "
        "sending. Don't sign off with a name -- just the message body."
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}],
        },
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    text_parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(text_parts).strip()


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
    # The QR is only ever shown on the welcome screen, which uses the warm
    # palette, so these match it rather than being tokenised -- a PNG is
    # generated in Python and cannot read a CSS variable.
    #
    # Whatever they are, they must stay DARK modules on a LIGHT field.
    # Scanners expect that polarity and light-on-dark QR codes fail
    # outright on a good number of phone cameras, so this must not be
    # inverted to suit a dark background.
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
    """Render the guest welcome screen.

    refresh_cache() calls this from the background refresh thread, which has
    no application context -- and render_template() requires one, both here
    and inside the frag() call that builds the Wi-Fi section. Entering the
    context here covers the whole body; nesting it inside a request is a
    no-op.
    """
    with app.app_context():
        return _render_welcome(g)


def _render_welcome(g):
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
        countdown_str = Markup(f"<b>{days_out}</b>&nbsp;days until {h(g['first_name'])}'s group arrives")

    if _WIFI_QR_DATA_URI:
        wifi_section = frag("wifi_section.html", 
            qr_data_uri=_WIFI_QR_DATA_URI,
            ssid=WIFI_SSID,
        )
    else:
        wifi_section = ""

    return render_template("welcome.html",
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


def build_stay_schedule(guest, check_in_time=None, check_out_time=None):
    """A date_range schedule matching the guest's stay. check_in_time/
    check_out_time ("HH:MM" strings) override the guest's own OwnerRez
    check-in/out times if given -- lets a host set a stricter/looser
    code-validity window than the official check-in/out times."""
    arrival, departure = guest["arrival"], guest["departure"]
    check_in_time = check_in_time or guest["check_in_time"]
    check_out_time = check_out_time or guest["check_out_time"]
    check_in_h, check_in_m = (int(x) for x in check_in_time.split(":"))
    check_out_h, check_out_m = (int(x) for x in check_out_time.split(":"))
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


def send_door_code_for_guest(device_id, guest, check_in_time=None, check_out_time=None):
    """Sends a code (last 4 of the guest's phone) valid for exactly their
    stay (or the given check_in_time/check_out_time override). Returns
    the sent code dict. Raises on any failure -- callers are expected to
    catch and show the real error, this is too consequential an action
    to fail silently."""
    phone = fetch_guest_phone(guest["guest_id"]) if guest.get("guest_id") else None
    code = phone_last4(phone)
    if not code:
        raise RuntimeError(
            f"No usable phone number on file for {guest['first_name']} "
            f"{guest['last_name']} -- can't derive a 4-digit code."
        )

    client = get_kwikset_client()
    slot = db_next_access_code_slot(device_id)
    schedule = build_stay_schedule(guest, check_in_time, check_out_time)
    guest_full_name = f"{guest['first_name']} {guest['last_name']}".strip()

    result = client.add_access_code(
        device_id=device_id, name=guest_full_name, code=code, slot=slot, schedule=schedule,
    )
    _db_safe(
        db_record_access_code, device_id, slot, guest["booking_key"], guest_full_name, code, schedule,
    )
    return result


def _is_schedule_expired(schedule):
    """schedule is the already-parsed dict (from json.loads on the stored
    schedule_json), not the raw string. Returns False (treated as "still
    active"/"unknown") for anything that isn't a date_range schedule with
    a parseable end time -- a code we can't confirm has expired is safer
    to keep showing as active than to hide."""
    if not schedule or schedule.get("type") != "date_range":
        return False
    end = schedule.get("end")
    if not end:
        return False
    try:
        end_dt = datetime.datetime(
            end["year"], end["month"], end["day"], end["hour"], end["minute"],
            tzinfo=ZoneInfo(POOL_TIMEZONE),
        )
    except (KeyError, ValueError, TypeError):
        return False
    return datetime.datetime.now(ZoneInfo(POOL_TIMEZONE)) > end_dt


def _format_schedule_window(schedule):
    if not schedule or schedule.get("type") != "date_range":
        return "No expiration set"
    start, end = schedule.get("start"), schedule.get("end")
    if not start or not end:
        return "—"
    try:
        start_d = datetime.date(start["year"], start["month"], start["day"])
        end_d = datetime.date(end["year"], end["month"], end["day"])
        start_t = f"{start['hour']:02d}:{start['minute']:02d}"
        end_t = f"{end['hour']:02d}:{end['minute']:02d}"
        return (f"{format_date(start_d)} {format_time_12h(start_t)} "
                f"&rarr; {format_date(end_d)} {format_time_12h(end_t)}")
    except (KeyError, ValueError, TypeError):
        return "—"


# ---------------------------------------------------------------------------
# 7. WEB SERVER
# ---------------------------------------------------------------------------
app = Flask(__name__)
# Jinja drops a template's final newline by default. These templates were
# lifted verbatim out of str.format() constants that did end with one, so
# keeping it makes the rendered bytes match what the app served before.
app.jinja_env.keep_trailing_newline = True
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure=True means the browser will only ever send this cookie over
    # HTTPS -- correct for Render (always HTTPS) but would break local
    # http://localhost testing, so it's conditional on not being a dev run.
    SESSION_COOKIE_SECURE=(os.environ.get("DISABLE_SECURE_COOKIE") != "1"),
)


def csrf_field():
    """Returns a hidden <input> to embed in every POST form. A session
    without one yet gets a fresh token generated on the spot."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return Markup(f'<input type="hidden" name="csrf_token" value="{token}">')


# Shared left-sidebar navigation, used on every admin page (not the
# guest-facing "/" welcome screen, and not /login or /setup). Built as
# a plain function returning ready-made HTML rather than a .format()
# template, so its CSS/content never needs brace-escaping.
_SIDEBAR_NAV_ITEMS = [
    ("/", "Welcome Screen"),
    ("/manage", "Manage Arrivals"),
    ("/cleaning", "Cleaning Checklist"),
    ("/pool", "Pool Control"),
    ("/doors", "Door Codes"),
    ("/messages", "Messages"),
    ("/users", "Users"),
]

def render_sidebar(active_path):
    username = h(session.get("username", ""))
    links_html = html_join(
        f'<a class="sidebar-link{" active" if path == active_path else ""}" href="{path}">{label}</a>'
        for path, label in _SIDEBAR_NAV_ITEMS
    )
    return Markup(f"""
<button class="hamburger-btn" onclick="document.getElementById('sidebar').classList.toggle('open'); document.getElementById('sidebar-backdrop').classList.toggle('open');" aria-label="Menu">&#9776;</button>
<div class="sidebar-backdrop" id="sidebar-backdrop" onclick="document.getElementById('sidebar').classList.remove('open'); this.classList.remove('open');"></div>
<nav class="sidebar" id="sidebar">
  <div class="sidebar-brand">Villa Brushy Creek</div>
  <div class="sidebar-nav">
    {links_html}
  </div>
  <div class="sidebar-footer">
    <div class="sidebar-user">Logged in as {username}</div>
    <form method="POST" action="/logout" class="sidebar-logout-form">{csrf_field()}<button type="submit" class="sidebar-logout-btn">Log out</button></form>
  </div>
</nav>
""")


# Paths reachable without being logged in. Exact matches only (not
# prefixes) -- deliberately narrow so a new route is protected by
# default unless explicitly added here.
_PUBLIC_PATHS = {"/login", "/setup", "/webhooks/ownerrez"}


@app.before_request
def _require_login():
    # The webhook receiver must always be reachable -- OwnerRez can and will
    # POST to it before anyone has even finished /setup, and it isn't a
    # session/browser request at all, so it's excluded before any of the
    # login/bootstrap logic below.
    if request.path == "/webhooks/ownerrez":
        return None

    # Static assets must be reachable while logged out, or the login and
    # setup pages -- which link the shared stylesheet -- render unstyled.
    # This sits above the bootstrap branch below because /setup needs it
    # too, before any password exists.
    #
    # This is the only prefix match in this function, and it stays safe
    # because Flask's /static route serves nothing but the files committed
    # under static/. Keep credentials out of that directory and it stays
    # true.
    if request.path.startswith("/static/"):
        return None

    # Bootstrap: nobody has a password set yet -> only /setup is reachable,
    # and everything else redirects there instead of to a login page that
    # nothing could actually pass.
    if not db_any_user_has_password():
        if request.path != "/setup":
            return redirect("/setup")
        return None

    if request.path in _PUBLIC_PATHS:
        return None

    if not session.get("user_id"):
        return redirect(f"/login?next={request.path}")

    if request.method == "POST":
        submitted = request.form.get("csrf_token")
        expected = session.get("csrf_token")
        if not submitted or not expected or submitted != expected:
            return ("Your session expired or this form was already submitted. "
                    "Go back and reload the page, then try again."), 400

    return None


def _no_cache(resp):
    # Prevent the tablet's browser (or any proxy/CDN in between) from
    # caching pages. Without this, a kiosk browser can keep showing a
    # stale copy even after it "reloads" -- it just re-serves cached
    # bytes instead of asking the server for fresh ones.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/setup", methods=["GET", "POST"])
def setup():
    # This route is reachable pre-login by design (see _require_login),
    # but only actually does anything while no user has a password set
    # yet -- once one exists, this becomes a dead end that just bounces
    # to /login, so it can't be used to create a second unauthenticated
    # backdoor after the real setup is done.
    if db_any_user_has_password():
        return redirect("/login")

    error = None
    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords don't match."
        else:
            admin = db_get_user_by_username(ADMIN_USERNAME)
            db_set_user_password(admin["id"], password)
            return redirect("/login")

    html = render_template("setup.html", 
        username=ADMIN_USERNAME,
        error_banner=Markup(f'<div class="error-banner">{h(error)}</div>') if error else "",
    )
    return _no_cache(Response(html, mimetype="text/html"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect("/menu")

    error = None
    next_path = request.values.get("next") or "/menu"
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/menu"  # never redirect off-site

    if request.method == "POST":
        username = request.form.get("username") or ""
        password = request.form.get("password") or ""
        user = verify_login(username, password)
        if user:
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(request.form.get("next") or "/menu")
        error = "Incorrect username or password."

    html = render_template("login.html", 
        error_banner=Markup(f'<div class="error-banner">{h(error)}</div>') if error else "",
        next_path=next_path,
    )
    return _no_cache(Response(html, mimetype="text/html"))


def _webhook_authorized():
    """Mirrors the shared-secret check from the original webhook receiver:
    no secret configured means no check (open), otherwise the header or
    query param must match exactly."""
    if not OWNERREZ_WEBHOOK_SECRET:
        return True
    supplied = request.headers.get("X-Webhook-Secret") or request.args.get("secret")
    return supplied == OWNERREZ_WEBHOOK_SECRET


@app.route("/webhooks/ownerrez", methods=["GET", "POST"])
def ownerrez_webhook():
    if request.method == "GET":
        # Some providers validate a new subscription with a GET challenge
        # before ever sending a real event.
        token = _extract_validation(request.args.to_dict())
        if token:
            return Response(token, mimetype="text/plain")
        with _cache_lock:
            open_count = len(db_list_open_messages())
        return {"ok": True, "service": "villa-brushy-creek-webhook", "open_messages": open_count}

    # POST
    if not _webhook_authorized():
        return {"ok": False, "error": "unauthorized"}, 401

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    # Subscription validation handshake -- echo the token, store nothing.
    token = _extract_validation(payload) or _extract_validation(request.args.to_dict())
    if token and not any(k in payload for k in ("entity", "resource", "data", "body")):
        return Response(token, mimetype="text/plain")

    event = parse_message_event(payload if isinstance(payload, dict) else {"raw": payload})
    try:
        event_id = db_add_message_event(event)
    except Exception as e:
        # Deliberately return a non-2xx here (not swallow-and-200) --
        # OwnerRez retries failed webhook deliveries automatically, and
        # losing a real inbound guest message silently would be worse
        # than a retry.
        print(f"[{datetime.datetime.now()}] Failed to store webhook message event: {e}", file=sys.stderr)
        return {"ok": False, "error": "storage failed"}, 500
    return {"ok": True, "stored_id": event_id, "category": event.get("category")}


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/login")


@app.route("/menu")
def menu():
    html = render_template("menu.html", 
        sidebar=render_sidebar("/menu"),
    )
    return _no_cache(Response(html, mimetype="text/html"))


@app.route("/messages")
def messages_page():
    error = None
    open_messages = []
    try:
        open_messages = db_list_open_messages()
    except Exception as e:
        error = str(e)

    ai_configured = bool(ANTHROPIC_API_KEY)
    webhook_url = f"{PUBLIC_BASE_URL.rstrip('/')}/webhooks/ownerrez" if PUBLIC_BASE_URL else ""

    subs_error = None
    subscriptions = []
    if OWNERREZ_USERNAME and OWNERREZ_TOKEN:
        try:
            subscriptions = ownerrez_list_webhook_subscriptions()
        except Exception as e:
            subs_error = str(e)

    message_subscribed = any(
        (s.get("category") or "").lower() == "message" for s in subscriptions
    )

    if not open_messages:
        cards_html = Markup('<p class="empty-state">No open guest messages right now.</p>')
    else:
        cards = []
        for row in open_messages:
            draft = row["draft_reply"] or ""
            ai_error = None
            if not draft and ai_configured:
                try:
                    draft = generate_ai_draft(row)
                    _db_safe(db_save_message_draft, row["id"], draft)
                except Exception as e:
                    ai_error = str(e)

            cards.append(frag("message_card.html", 
                event_id=row["id"],
                guest=row["guest"] or "Unknown guest",
                received=row["received_utc"] or "",
                body=row["body"],
                draft=draft,
                ai_note=(Markup(f'<div class="ai-note">AI draft unavailable: {h(ai_error)}</div>') if ai_error else ""),
                csrf_field=csrf_field(),
            ))
        cards_html = html_join(cards)

    html = render_template("messages.html", 
        error_banner=Markup(f'<div class="error-banner">{h(error)}</div>') if error else "",
        cards=cards_html,
        webhook_url=webhook_url or "Set PUBLIC_BASE_URL to see your real webhook URL here.",
        subscribed_label="Subscribed" if message_subscribed else "Not subscribed",
        subscribed_class="status-on" if message_subscribed else "status-off",
        subs_error_banner=(Markup(f'<div class="error-banner">{h(subs_error)}</div>') if subs_error else ""),
        ai_status_label="Configured" if ai_configured else "Not configured",
        ai_status_class="status-on" if ai_configured else "status-off",
        csrf_field=csrf_field(),
        sidebar=render_sidebar("/messages"),
    )
    return _no_cache(Response(html, mimetype="text/html"))


@app.route("/messages/setup_webhook", methods=["POST"])
def messages_setup_webhook():
    if not PUBLIC_BASE_URL:
        return "Set PUBLIC_BASE_URL (your real Render URL) before registering the webhook.", 400
    url = f"{PUBLIC_BASE_URL.rstrip('/')}/webhooks/ownerrez"
    try:
        ownerrez_create_webhook_subscription(url, "message")
    except Exception as e:
        return f"Failed to register webhook subscription: {e}", 500
    return redirect("/messages")


@app.route("/messages/regenerate", methods=["POST"])
def messages_regenerate():
    event_id = request.form.get("event_id")
    if not event_id:
        return "Missing event_id", 400
    row = db_get_message_event(int(event_id))
    if row is None:
        return "Message not found", 404
    try:
        draft = generate_ai_draft(row)
    except Exception as e:
        return f"Failed to generate draft: {e}", 500
    _db_safe(db_save_message_draft, int(event_id), draft)
    return redirect(f"/messages#msg-{event_id}")


@app.route("/messages/save_draft", methods=["POST"])
def messages_save_draft():
    event_id = request.form.get("event_id")
    draft_text = request.form.get("draft_text", "")
    if not event_id:
        return "Missing event_id", 400
    db_save_message_draft(int(event_id), draft_text)
    return redirect(f"/messages#msg-{event_id}")


@app.route("/messages/send", methods=["POST"])
def messages_send():
    event_id = request.form.get("event_id")
    reply_text = request.form.get("draft_text", "")
    if not event_id:
        return "Missing event_id", 400
    if not reply_text or not reply_text.strip():
        return "Reply text can't be empty", 400
    row = db_get_message_event(int(event_id))
    if row is None:
        return "Message not found", 404
    if not row["thread_id"]:
        return "This message has no thread_id -- can't send a reply to it.", 400

    try:
        ownerrez_send_message(row["thread_id"], reply_text)
    except Exception as e:
        # Save the edited text as the draft even on failure, so the host
        # doesn't lose their edit and can just retry.
        _db_safe(db_save_message_draft, int(event_id), reply_text)
        return f"Failed to send reply: {e}", 500

    db_mark_message_handled(int(event_id), handled=True, sent=True)
    return redirect("/messages")


@app.route("/messages/skip", methods=["POST"])
def messages_skip():
    event_id = request.form.get("event_id")
    if not event_id:
        return "Missing event_id", 400
    db_mark_message_handled(int(event_id), handled=True, sent=False)
    return redirect("/messages")


@app.route("/users")
def users_page():
    users = db_list_users()
    rows = html_join(
        frag("user_row.html", 
            user_id=u["id"],
            username=u["username"],
            status_label="Active" if u["password_hash"] else "Awaiting first login",
            status_class="status-on" if u["password_hash"] else "status-off",
            csrf_field=csrf_field(),
            self_marker=" (you)" if u["id"] == session.get("user_id") else "",
            delete_disabled="disabled" if len(users) <= 1 else "",
        )
        for u in users
    )
    html = render_template("users.html", 
        rows=rows,
        csrf_field=csrf_field(),
        sidebar=render_sidebar("/users"),
    )
    return _no_cache(Response(html, mimetype="text/html"))


@app.route("/users/add", methods=["POST"])
def users_add():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not username:
        return "Username is required", 400
    if len(password) < 8:
        return "Password must be at least 8 characters", 400
    if db_get_user_by_username(username):
        return "That username is already taken", 400
    db_create_user(username, password)
    return redirect("/users")


@app.route("/users/delete", methods=["POST"])
def users_delete():
    user_id = request.form.get("user_id")
    if not user_id:
        return "Missing user_id", 400
    if len(db_list_users()) <= 1:
        return "Can't delete the last remaining user -- that would lock everyone out.", 400
    db_delete_user(int(user_id))
    if session.get("user_id") == int(user_id):
        session.clear()
        return redirect("/login")
    return redirect("/users")


@app.route("/users/reset_password", methods=["POST"])
def users_reset_password():
    user_id = request.form.get("user_id")
    password = request.form.get("password") or ""
    if not user_id:
        return "Missing user_id", 400
    if len(password) < 8:
        return "Password must be at least 8 characters", 400
    db_set_user_password(int(user_id), password)
    return redirect("/users")


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
            html = render_template("error.html", 
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
            row_parts.append(frag("manage_row.html", 
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
                csrf_field=csrf_field(),
            ))
        rows_html = html_join(row_parts)

    html = render_template("manage.html", 
        rows=rows_html,
        mode_auto_class="mode-active" if mode == "auto" else "",
        mode_manual_class="mode-active" if mode == "manual" else "",
        last_updated=last_updated or "never",
        upcoming_count_label=len(upcoming),
        error_banner=(
            Markup(f'<div class="error-banner">Last refresh failed: {h(last_error)}</div>')
            if last_error else ""
        ),
        csrf_field=csrf_field(),
        sidebar=render_sidebar("/manage"),
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

            checkbox_rows = html_join(
                frag("cleaning_checkbox.html", 
                    booking_key=g["booking_key"],
                    task_name=task_name,
                    checked="checked" if checked else "",
                    done_class="task-done" if checked else "",
                    csrf_field=csrf_field(),
                )
                for task_name, checked in task_state.items()
            )

            cards.append(frag("cleaning_card.html", 
                booking_key=g["booking_key"],
                first_name=g["first_name"],
                arrival_str=format_date(g["arrival"]),
                departure_str=format_date(g["departure"]),
                done_count=done_count,
                total=total,
                progress_pct=int(100 * done_count / total) if total else 0,
                ready_badge=Markup('<span class="ready-badge">Ready ✓</span>') if all_done else "",
                card_class="cleaning-card-done" if all_done else "",
                checkbox_rows=checkbox_rows,
                csrf_field=csrf_field(),
            ))
        cards_html = html_join(cards)

    html = render_template("cleaning.html", 
        cards=cards_html,
        last_updated=last_updated or "never",
        error_banner=(
            Markup(f'<div class="error-banner">Last refresh failed: {h(last_error)}</div>')
            if last_error else ""
        ),
        csrf_field=csrf_field(),
        sidebar=render_sidebar("/cleaning"),
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
        html = render_template("pool.html", 
            system_name=PROPERTY_DISPLAY_NAME,
            online_badge="",
            error_banner=Markup(f'<div class="error-banner">{h(error)}</div>'),
            sensor_cards="",
            setpoint_cards="",
            equipment_cards="",
            schedule_rows=Markup('<p class="empty-state">Pool control must be working to manage schedules.</p>'),
            device_options="",
            csrf_field=csrf_field(),
            sidebar=render_sidebar("/pool"),
        )
        return _no_cache(Response(html, mimetype="text/html"))

    try:
        sensors, setpoints, equipment = classify_pool_devices(snapshot["devices"])

        sensor_html = html_join(
            frag("pool_sensor_card.html", label=s["label"], value=s["state"])
            for s in sensors
        )
        setpoint_html = html_join(
            frag("pool_setpoint_card.html", 
                key=s["key"],
                label=s["label"],
                value=s["state"] or "—",
                status_label="Heating enabled" if s["is_on"] else "Heating off",
                status_class="status-on" if s["is_on"] else "status-off",
                csrf_field=csrf_field(),
            )
            for s in setpoints
        )
        equipment_html = html_join(
            frag("pool_equipment_card.html", 
                key=e["key"],
                label=e["label"],
                status_label="On" if e["is_on"] else "Off",
                status_class="status-on" if e["is_on"] else "status-off",
                card_class="equipment-card-on" if e["is_on"] else "",
                button_label="Turn off" if e["is_on"] else "Turn on",
                csrf_field=csrf_field(),
            )
            for e in equipment
        )

        # Schedule section: dropdown of real equipment devices to schedule,
        # plus the list of existing schedules.
        device_options = html_join(
            f'<option value="{h(e["key"])}" data-label="{h(e["label"])}">{h(e["label"])}</option>'
            for e in equipment
        )

        with _cache_lock:
            schedules = list(_cache["pool_schedules"].items())
        schedules.sort(key=lambda kv: (kv[1]["device_label"], kv[1]["on_time"]))

        if schedules:
            schedule_rows = html_join(
                frag("pool_schedule_row.html", 
                    schedule_id=sid,
                    device_label=s["device_label"],
                    on_time=_format_time_12h_str(s["on_time"]),
                    off_time=_format_time_12h_str(s["off_time"]),
                    days_label=_format_days_label(s["days"]),
                    status_label="Enabled" if s["enabled"] else "Disabled",
                    status_class="status-on" if s["enabled"] else "status-off",
                    row_class="" if s["enabled"] else "schedule-row-disabled",
                    toggle_label="Disable" if s["enabled"] else "Enable",
                    csrf_field=csrf_field(),
                )
                for sid, s in schedules
            )
        else:
            schedule_rows = Markup('<p class="empty-state">No schedules set up yet.</p>')

        html = render_template("pool.html", 
            system_name=snapshot["system_name"],
            online_badge=(
                Markup('<span class="online-badge online-yes">Online</span>') if snapshot["online"]
                else Markup('<span class="online-badge online-no">Offline</span>') if snapshot["online"] is False
                else ""
            ),
            error_banner="",
            sensor_cards=sensor_html or Markup('<p class="empty-state">No sensor readings available.</p>'),
            setpoint_cards=setpoint_html,
            equipment_cards=equipment_html or Markup('<p class="empty-state">No controllable equipment found.</p>'),
            schedule_rows=schedule_rows,
            device_options=device_options or Markup('<option value="">No equipment available</option>'),
            csrf_field=csrf_field(),
            sidebar=render_sidebar("/pool"),
        )
    except Exception as e:
        # Belt-and-suspenders: a fetch can succeed but return data shaped
        # slightly differently than expected (e.g. an unexpected device
        # attribute). Show the real error instead of a blank 500 page.
        print(f"[{datetime.datetime.now()}] /pool render error: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        html = render_template("pool.html", 
            system_name=PROPERTY_DISPLAY_NAME,
            online_badge="",
            error_banner=Markup(f'<div class="error-banner">Error building pool page: '
                                f'{h(type(e).__name__)}: {h(e)}</div>'),
            sensor_cards="",
            setpoint_cards="",
            equipment_cards="",
            schedule_rows="",
            device_options="",
            csrf_field=csrf_field(),
            sidebar=render_sidebar("/pool"),
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


DEFAULT_DOOR_CHECK_IN = "15:10"
DEFAULT_DOOR_CHECK_OUT = "11:30"

# Half-hour grid, plus whichever defaults are configured above.
#
# The defaults MUST appear in this list. doors() falls back to them when a
# submitted value isn't a member, and _time_options_html() marks an option
# "selected" only on an exact match -- so a default that isn't in the grid
# leaves the <select> with nothing selected and the browser silently shows
# the first entry, 12:00 AM. A 3:10pm check-in is not on a half-hour
# boundary, so it is unioned in here rather than the grid being widened to
# ten-minute steps, which would make this a 144-item dropdown on a phone.
_TIME_OPTION_VALUES = sorted(
    {f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)}
    | {DEFAULT_DOOR_CHECK_IN, DEFAULT_DOOR_CHECK_OUT}
)


def _time_options_html(selected_value):
    return html_join(
        f'<option value="{t}" {"selected" if t == selected_value else ""}>{format_time_12h(t)}</option>'
        for t in _TIME_OPTION_VALUES
    )


@app.route("/doors")
def doors():
    today = datetime.date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
    except ValueError:
        year, month = today.year, today.month

    default_checkin = request.args.get("default_checkin", DEFAULT_DOOR_CHECK_IN)
    default_checkout = request.args.get("default_checkout", DEFAULT_DOOR_CHECK_OUT)
    if default_checkin not in _TIME_OPTION_VALUES:
        default_checkin = DEFAULT_DOOR_CHECK_IN
    if default_checkout not in _TIME_OPTION_VALUES:
        default_checkout = DEFAULT_DOOR_CHECK_OUT

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

    lock_options = html_join(
        f'<option value="{h(lk["device_id"])}" {"selected" if lk["device_id"] == selected_device_id else ""}>'
        f'{h(lk["name"])} ({h(lk["home"])})</option>'
        for lk in locks
    )
    month_options = html_join(
        f'<option value="{y}-{m:02d}" {"selected" if sel else ""}>{label}</option>'
        for y, m, label, sel in _month_options(year, month)
    )

    if guest_error:
        guest_rows = Markup(f'<p class="empty-state">Couldn\'t load bookings: {h(guest_error)}</p>')
    elif not guests:
        guest_rows = Markup('<p class="empty-state">No guests arriving this month.</p>')
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

            deposit = fetch_deposit_status(g["booking_id"]) if g.get("booking_id") else None
            if deposit is None:
                deposit_label, deposit_class = "Unknown", "status-off"
            elif deposit["received"]:
                amt = deposit.get("amount")
                deposit_label = f"Received (${amt:.0f})" if amt else "Received"
                deposit_class = "status-on"
            else:
                deposit_label, deposit_class = "Not received", "status-off"

            if g.get("agreement_signed"):
                agreement_label, agreement_class = "Signed", "status-on"
            else:
                agreement_label, agreement_class = "Not signed", "status-off"

            rows.append(frag("doors_row.html", 
                first_name=g["first_name"],
                last_name=g["last_name"],
                arrival_str=format_date(g["arrival"]),
                departure_str=format_date(g["departure"]),
                last4=last4,
                status_label=status_label,
                status_class=status_class,
                deposit_label=deposit_label,
                deposit_class=deposit_class,
                agreement_label=agreement_label,
                agreement_class=agreement_class,
                booking_key=g["booking_key"],
                selected_device_id_for_row=selected_device_id or "",
                year_for_row=year,
                month_for_row=month,
                checkin_options=_time_options_html(default_checkin),
                checkout_options=_time_options_html(default_checkout),
                send_disabled="disabled" if (existing or not selected_device_id or last4 == "—") else "",
                send_label="Already sent" if existing else "Send code",
                csrf_field=csrf_field(),
            ))
        guest_rows = html_join(rows)

    # All-codes audit section, independent of the month/lock filters above --
    # this is meant to show everything this app has ever sent, across all
    # locks, so a stale/expired code isn't hidden just because you're
    # looking at a different month right now.
    lock_name_by_id = {lk["device_id"]: f'{lk["name"]} ({lk["home"]})' for lk in locks}
    all_codes = db_list_all_access_codes()
    if not all_codes:
        all_codes_rows = Markup('<p class="empty-state">No door codes have been sent yet.</p>')
    else:
        code_rows = []
        for row in all_codes:
            try:
                schedule = json.loads(row["schedule_json"]) if row["schedule_json"] else None
            except (json.JSONDecodeError, TypeError):
                schedule = None
            expired = _is_schedule_expired(schedule)
            code_rows.append(frag("all_codes_row.html", 
                lock_label=lock_name_by_id.get(row["device_id"], row["device_id"]),
                guest_name=row["guest_name"] or "—",
                code=row["code"] or "—",
                slot=row["slot"],
                window_str=_format_schedule_window(schedule),
                expired_label="Expired" if expired else "Active",
                expired_class="status-off" if expired else "status-on",
                row_class="expired-row" if expired else "",
                device_id=row["device_id"],
                csrf_field=csrf_field(),
            ))
        all_codes_rows = html_join(code_rows)

    html = render_template("doors.html", 
        error_banner=Markup(f'<div class="error-banner">{h(error)}</div>') if error else "",
        lock_options=lock_options or Markup('<option value="">No locks found</option>'),
        month_options=month_options,
        default_checkin_options=_time_options_html(default_checkin),
        default_checkout_options=_time_options_html(default_checkout),
        guest_rows=guest_rows,
        all_codes_rows=all_codes_rows,
        selected_device_id=selected_device_id or "",
        selected_month_value=f"{year}-{month:02d}",
        selected_year=year,
        selected_month=month,
        csrf_field=csrf_field(),
        sidebar=render_sidebar("/doors"),
    )
    return _no_cache(Response(html, mimetype="text/html"))


@app.route("/doors/send", methods=["POST"])
def doors_send():
    device_id = request.form.get("device_id")
    booking_key = request.form.get("booking_key")
    year = request.form.get("year")
    month = request.form.get("month")
    check_in_time = request.form.get("check_in_time")
    check_out_time = request.form.get("check_out_time")
    if not device_id or not booking_key:
        return "Missing device_id or booking_key", 400
    try:
        year, month = int(year), int(month)
    except (TypeError, ValueError):
        return "Missing or invalid year/month", 400
    if check_in_time not in _TIME_OPTION_VALUES or check_out_time not in _TIME_OPTION_VALUES:
        return "Invalid check-in/check-out time", 400

    try:
        guests = fetch_bookings_for_month(year, month)
        guest = next((g for g in guests if g["booking_key"] == booking_key), None)
        if guest is None:
            return "Guest not found for that month -- try reloading the page", 404
        send_door_code_for_guest(device_id, guest, check_in_time, check_out_time)
    except Exception as e:
        return f"Failed to send door code: {e}", 500

    return redirect(f"/doors?device_id={device_id}&year={year}&month={month}")


@app.route("/doors/remove", methods=["POST"])
def doors_remove():
    device_id = request.form.get("device_id")
    slot = request.form.get("slot")
    if not device_id or not slot:
        return "Missing device_id or slot", 400
    try:
        slot = int(slot)
    except ValueError:
        return "Invalid slot", 400

    try:
        client = get_kwikset_client()
        client.remove_access_code(device_id, slot)
    except Exception as e:
        # Deliberately do NOT delete our own tracking record if the real
        # removal failed -- our record should only stop reflecting reality
        # once we've actually confirmed the lock-side removal succeeded.
        return f"Failed to remove door code: {e}", 500

    _db_safe(db_delete_access_code_record, device_id, slot)
    return redirect("/doors#all-codes")


@app.route("/doors/manual_add", methods=["POST"])
def doors_manual_add():
    device_id = request.form.get("device_id")
    name = (request.form.get("name") or "").strip()
    code = request.form.get("code") or ""
    never_expire = request.form.get("never_expire") == "on"

    if not device_id or not name:
        return "Missing device_id or name", 400

    schedule = None
    if not never_expire:
        start_date = request.form.get("start_date")
        start_time = request.form.get("start_time")
        end_date = request.form.get("end_date")
        end_time = request.form.get("end_time")
        if not all([start_date, start_time, end_date, end_time]):
            return "Start/end date and time are required unless 'Never expires' is checked", 400
        try:
            sy, sm, sd = (int(x) for x in start_date.split("-"))
            sh, smin = (int(x) for x in start_time.split(":"))
            ey, em, ed = (int(x) for x in end_date.split("-"))
            eh, emin = (int(x) for x in end_time.split(":"))
        except ValueError:
            return "Invalid date/time format", 400
        schedule = {
            "type": "date_range",
            "start": {"year": sy, "month": sm, "day": sd, "hour": sh, "minute": smin},
            "end": {"year": ey, "month": em, "day": ed, "hour": eh, "minute": emin},
        }

    try:
        client = get_kwikset_client()
        slot = db_next_access_code_slot(device_id)
        friendly_name = name[:14]
        client.add_access_code(device_id=device_id, name=friendly_name, code=code, slot=slot, schedule=schedule)
    except Exception as e:
        return f"Failed to create door code: {e}", 500

    # booking_key is None here -- this code isn't tied to any guest
    # booking, so there's nothing to match it against in the guest
    # table above. It still shows up correctly in the "All Door Codes"
    # table below, same as any guest-sent code.
    _db_safe(db_record_access_code, device_id, slot, None, friendly_name, code, schedule)
    return redirect("/doors#all-codes")


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
