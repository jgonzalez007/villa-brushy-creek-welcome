"""Render every page with seeded data and dump the HTML, for before/after diffing.

    python capture.py <outdir>

Used to prove the Jinja migration is behaviour-neutral. Seeds enough rows
that the list-rendering branches (message cards, door codes, users) produce
real output instead of empty states.
"""
import os
import pathlib
import sqlite3
import sys
import tempfile

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "capture")
DB = pathlib.Path(tempfile.mkdtemp()) / "cap.db"
os.environ["DB_PATH"] = str(DB)
os.environ["SECRET_KEY"] = "capture-only"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["WIFI_SSID"] = "VillaWifi"
os.environ["WIFI_PASSWORD"] = "hunter2"
os.environ["WIFI_AUTH"] = "WPA"

# Keep the capture hermetic -- no live OwnerRez/Kwikset/iAqualink calls.
for _v in ("OWNERREZ_USERNAME", "OWNERREZ_TOKEN", "IAQUALINK_USERNAME",
           "IAQUALINK_PASSWORD", "KWIKSET_EMAIL", "KWIKSET_REFRESH_TOKEN",
           "ANTHROPIC_API_KEY"):
    os.environ.pop(_v, None)

import app as m  # noqa: E402

PAGES = ["/", "/menu", "/messages", "/pool", "/doors", "/cleaning",
         "/users", "/manage", "/status"]


def seed():
    db = sqlite3.connect(DB)
    # Two messages: one with a plain name, one with markup, so escaping
    # behaviour shows up in the diff rather than hiding.
    for guest, body, draft in [
        ("Dana Whitfield", "What time is check-in?", "Check-in is at 4pm."),
        ("Bobby <script>alert(1)</script>", "hi <b>there</b> & goodbye", ""),
    ]:
        db.execute(
            "INSERT INTO message_events (received_utc, category, action, guest,"
            " body, is_incoming, handled, draft_reply) VALUES (?,?,?,?,?,1,0,?)",
            ("2026-09-26T12:00:00", "message", "created", guest, body, draft),
        )
    for slot, name in ((5, "Carol O'Brien"), (6, 'Ann "Q" <b>Lee</b>')):
        db.execute(
            "INSERT INTO kwikset_access_codes (device_id, slot, booking_key,"
            " guest_name, code, schedule_json, created_at) VALUES (?,?,?,?,?,?,?)",
            ("dev1", slot, f"bk{slot}", name, "1234", "{}", "2026-09-26T00:00:00"),
        )
    db.execute(
        "INSERT INTO pool_schedules (id, device_key, device_label, on_time,"
        " off_time, days, enabled) VALUES (?,?,?,?,?,?,1)",
        ("sched1", "pool_pump", "Pool Pump", "08:00", "18:00", "Mon,Tue"),
    )
    db.commit()
    db.close()


def seed_bookings():
    """Populate the guest cache so / renders TEMPLATE rather than ERROR_TEMPLATE.

    Built by pushing a synthetic OwnerRez payload through the app's own
    _booking_to_guest_dict, so the cached shape stays correct as that
    function changes rather than being hand-rolled here.
    """
    bookings = [
        {"id": 9001, "arrival": "2026-10-02", "departure": "2026-10-06",
         "adults": 2, "children": 1, "platform": "airbnb",
         "platform_reservation_number": "HMABC123",
         "guest": {"id": 1, "first_name": "Dana", "last_name": "Whitfield"},
         "property": {"name": "Villa Brushy Creek"},
         "agreements": [{"date": "2026-09-20", "name": "Rental agreement"}]},
        {"id": 9002, "arrival": "2026-10-11", "departure": "2026-10-14",
         "adults": 4, "children": 0, "platform": "vrbo",
         "platform_reservation_number": "VR-77",
         "guest": {"id": 2, "first_name": "Bobby <script>alert(1)</script>",
                   "last_name": 'O"Brien & Sons'},
         "property": {"name": "Villa Brushy Creek"},
         "agreements": []},
    ]
    guests = [m._booking_to_guest_dict(b) for b in bookings]
    with m._cache_lock:
        m._cache["upcoming"] = guests
        m._cache["selected_key"] = guests[0]["booking_key"]
        m._cache["mode"] = "auto"
        m._cache["last_updated"] = "2026-09-26 12:00:00"
        m._cache["last_error"] = None
    m._recompute_selected_and_render()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    c = m.app.test_client()
    c.post("/setup", data={"password": "testpass123", "confirm": "testpass123"})
    c.post("/login", data={"username": "admin", "password": "testpass123"})
    for step in (seed, seed_bookings):
        try:
            step()
        except Exception as e:                  # a seed gap shouldn't abort
            print(f"seed warning in {step.__name__}: {type(e).__name__}: {e}")
    c.post("/users/add", data={"username": "cleaner", "password": "cleanpass123"})

    # Logged out
    for name, path in (("login", "/login"), ("appcss", "/static/app.css")):
        c2 = m.app.test_client()
        (OUT / f"{name}.html").write_bytes(c2.get(path).get_data())

    for p in PAGES:
        r = c.get(p)
        name = "root" if p == "/" else p.strip("/").replace("/", "_")
        (OUT / f"{name}.html").write_bytes(r.get_data())
        print(f"  {p:<11} {r.status_code}  {len(r.get_data()):>7}b")


if __name__ == "__main__":
    main()
