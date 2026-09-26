"""Regression test: guest-controlled text must not reach the browser as markup.

Run against main and against the fix branch. On main this is expected to
FAIL, which is the point -- it demonstrates the vulnerability rather than
asserting it.

    python test_xss.py
"""
import os
import pathlib
import sqlite3
import sys
import tempfile

DB = pathlib.Path(tempfile.mkdtemp()) / "xss.db"
os.environ["DB_PATH"] = str(DB)
os.environ["SECRET_KEY"] = "test-only"
os.environ["ADMIN_USERNAME"] = "admin"

# Importing app starts its background refresh threads immediately. Clear the
# integration credentials first so a test run cannot reach OwnerRez, iAqualink
# or Kwikset -- otherwise running the suite on a configured machine pulls real
# guest data over the network.
for _var in ("OWNERREZ_USERNAME", "OWNERREZ_TOKEN", "IAQUALINK_USERNAME",
             "IAQUALINK_PASSWORD", "KWIKSET_EMAIL", "KWIKSET_REFRESH_TOKEN",
             "ANTHROPIC_API_KEY"):
    os.environ.pop(_var, None)

import app as appmod  # noqa: E402  (env must be set first)

PAYLOAD = '<script>alert(1)</script>'
IMG = '<img src=x onerror=alert(2)>'


def seed():
    db = sqlite3.connect(DB)
    db.execute(
        "INSERT INTO message_events (received_utc, category, action, guest, body,"
        " is_incoming, handled) VALUES (?,?,?,?,?,1,0)",
        ("2026-09-26T00:00:00", "message", "created", f"Bobby {PAYLOAD}", f"hello {IMG}"),
    )
    db.execute(
        "INSERT INTO kwikset_access_codes (device_id, slot, booking_key, guest_name,"
        " code, schedule_json, created_at) VALUES (?,?,?,?,?,?,?)",
        ("dev1", 5, "bk1", f"Carol {PAYLOAD}", "1234", "{}", "2026-09-26T00:00:00"),
    )
    db.commit()
    db.close()


def main():
    c = appmod.app.test_client()
    c.post("/setup", data={"password": "testpass123", "confirm": "testpass123"})
    c.post("/login", data={"username": "admin", "password": "testpass123"})
    seed()

    failures = []
    for path in ("/messages", "/doors"):
        html = c.get(path).get_data(as_text=True)
        for name, raw in (("<script>", PAYLOAD), ("<img onerror>", IMG)):
            if raw in html:
                failures.append(f"{path}: raw {name} present in response")
        # The text should still be visible, just inert.
        if "&lt;script&gt;" not in html and path == "/messages":
            failures.append(f"{path}: payload neither escaped nor present -- check the test")

    h = getattr(appmod, "h", None)
    if h is None:
        print("note: app has no h() helper (pre-fix revision)")
    else:
        print(f"h() escapes quotes: {h('a\"b') == 'a&quot;b'}")
        print(f"h(None) -> {h(None)!r}")
    if failures:
        print("\nVULNERABLE:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nPASS: no raw guest-controlled markup in /messages or /doors")


if __name__ == "__main__":
    main()
