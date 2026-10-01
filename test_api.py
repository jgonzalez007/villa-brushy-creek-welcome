"""Tests for the read-only /api/messages/open endpoint used by OpenClaw.

    python test_api.py

Covers the three things most likely to break quietly:

  1. Auth. A wrong or missing token must be a 401, never a 302 to /login --
     a polling client follows the redirect, gets the login page, finds no
     messages in it and reports "nothing new" forever instead of erroring.
  2. Enablement. With no OPENCLAW_API_TOKEN the surface must 404, including
     before /setup has been completed.
  3. The shared draft. The API must return the draft stored against the
     event -- the same text /messages will send -- not a fresh one.
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

DB = pathlib.Path(tempfile.mkdtemp()) / "api.db"
os.environ["DB_PATH"] = str(DB)
os.environ["SECRET_KEY"] = "test-only"
os.environ["ADMIN_USERNAME"] = "admin"
TOKEN = "test-token-not-a-real-secret"
os.environ["OPENCLAW_API_TOKEN"] = TOKEN
os.environ["PUBLIC_BASE_URL"] = "https://example.invalid"

# Importing app starts its background refresh threads immediately. Clear the
# integration credentials first so a test run cannot reach OwnerRez, iAqualink
# or Kwikset -- otherwise running the suite on a configured machine pulls real
# guest data over the network. ANTHROPIC_API_KEY included: with it set, the
# draft assertions below would make a live billed API call.
for _var in ("OWNERREZ_USERNAME", "OWNERREZ_TOKEN", "IAQUALINK_USERNAME",
             "IAQUALINK_PASSWORD", "KWIKSET_EMAIL", "KWIKSET_REFRESH_TOKEN",
             "ANTHROPIC_API_KEY"):
    os.environ.pop(_var, None)

import app as appmod  # noqa: E402  (env must be set first)

STORED_DRAFT = "Hi Bobby -- checkin is at [confirm time]."

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
        failures.append(name)


def seed():
    db = sqlite3.connect(DB)
    # draft_reply pre-set: the endpoint must hand back this exact text.
    # A regenerated draft would differ, and the host would then approve
    # one wording in chat and send another from the web UI.
    db.execute(
        "INSERT INTO message_events (received_utc, category, action, guest, body,"
        " thread_id, booking_id, draft_reply, is_incoming, handled)"
        " VALUES (?,?,?,?,?,?,?,?,1,0)",
        ("2026-09-26T00:00:00", "thread_message", "entity_create", "Bobby Tables",
         "What time is checkin?", 9911, 4242, STORED_DRAFT),
    )
    # A handled message must not appear -- otherwise every poll re-notifies
    # about messages already dealt with.
    db.execute(
        "INSERT INTO message_events (received_utc, category, action, guest, body,"
        " thread_id, handled) VALUES (?,?,?,?,?,?,1)",
        ("2026-09-25T00:00:00", "thread_message", "entity_create", "Done Already",
         "old message", 9912),
    )
    db.commit()
    db.close()


def main():
    c = appmod.app.test_client()

    print("\nbefore /setup (no password set yet):")
    r = c.get("/api/messages/open")
    # The bootstrap branch redirects everything to /setup; the API must be
    # exempt from that too, or a fresh deploy answers polls with a 302.
    check("bad token -> 401 not 302", r.status_code == 401,
          f"got {r.status_code}")

    # werkzeug hashes with scrypt, which is absent from a Python built
    # against a libressl/openssl lacking it (notably macOS system Python
    # 3.9). Report whether the browser session was really established, so
    # "session cookie alone -> 401" below can't silently pass just because
    # login never happened.
    c.post("/setup", data={"password": "testpass123", "confirm": "testpass123"})
    c.post("/login", data={"username": "admin", "password": "testpass123"})
    logged_in = c.get("/messages").status_code == 200
    print(f"\nbrowser session established: {logged_in}"
          + ("" if logged_in else "  (hashlib.scrypt unavailable in this Python"
                                  " -- the session assertion below is weaker)"))
    seed()

    print("\nauth:")
    check("no header -> 401", c.get("/api/messages/open").status_code == 401)
    check("wrong token -> 401",
          c.get("/api/messages/open",
                headers={"Authorization": "Bearer wrong"}).status_code == 401)
    check("token as query param -> 401 (header only)",
          c.get(f"/api/messages/open?token={TOKEN}").status_code == 401)
    r = c.get("/api/messages/open", headers={"Authorization": f"Bearer {TOKEN}"})
    check("correct token -> 200", r.status_code == 200, f"got {r.status_code}")
    # A logged-in browser session must not be enough on its own: the token
    # is the only credential for /api, so leaking it is the only exposure.
    check("session cookie alone -> 401",
          c.get("/api/messages/open").status_code == 401)

    print("\npayload:")
    if r.status_code != 200:
        print("  (skipped -- endpoint did not return 200)")
    else:
        data = json.loads(r.get_data(as_text=True))
        check("only unhandled messages returned", data["count"] == 1,
              f"count={data['count']}")
        m = data["messages"][0]
        check("stored draft returned verbatim", m["draft_reply"] == STORED_DRAFT,
              f"got {m['draft_reply']!r}")
        check("guest name present", m["guest"] == "Bobby Tables")
        # String, not int: message_events declares thread_id/booking_id TEXT.
        check("thread_id present, as stored text", m["thread_id"] == "9911",
              f"got {m['thread_id']!r}")
        check("booking_id present, as stored text", m["booking_id"] == "4242",
              f"got {m['booking_id']!r}")
        check("body present", m["body"] == "What time is checkin?")
        check("deep link absolute via PUBLIC_BASE_URL",
              m["url"] == "https://example.invalid/messages#msg-1",
              f"got {m['url']!r}")
        check("ai_configured reports false with no key",
              data["ai_configured"] is False)
        check("no draft_error on a pre-stored draft",
              m["draft_error"] is None, f"got {m['draft_error']!r}")

    print("\nno send route is exposed under /api:")
    for path in ("/api/messages/send", "/api/messages/1/send"):
        got = c.post(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        check(f"POST {path} -> 404/405", got in (404, 405), f"got {got}")

    print("\nwith OPENCLAW_API_TOKEN unset:")
    appmod.API_TOKEN = None
    r = c.get("/api/messages/open", headers={"Authorization": f"Bearer {TOKEN}"})
    check("-> 404 (surface disabled)", r.status_code == 404, f"got {r.status_code}")
    appmod.API_TOKEN = TOKEN

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures))
        sys.exit(1)
    print("PASS: all API checks")


if __name__ == "__main__":
    main()
