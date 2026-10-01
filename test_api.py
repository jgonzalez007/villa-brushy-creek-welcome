"""Tests for the /api/messages endpoints used by OpenClaw.

    python test_api.py

Covers the things most likely to break quietly:

  1. Auth. A wrong or missing token must be a 401, never a 302 to /login --
     a polling client follows the redirect, gets the login page, finds no
     messages in it and reports "nothing new" forever instead of erroring.
  2. Enablement. With no OPENCLAW_API_TOKEN the surface must 404, including
     before /setup has been completed.
  3. The shared draft. The API must return the draft stored against the
     event -- the same text /messages will send -- not a fresh one.
  4. Pushing a draft. POST .../draft must store text the host will send,
     reject input that would silently destroy a draft (empty) or land on a
     message already dealt with (handled), and never send anything itself.
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
    # Regression: a non-ASCII token used to be a 500, not a 401.
    # secrets.compare_digest raises TypeError on str operands containing
    # non-ASCII characters, so the header -- which an unauthenticated
    # caller fully controls -- crashed the before_request hook and got
    # Werkzeug's stock HTML error page instead of the JSON 401 body. Both
    # a latin-1 char and a wider codepoint, since they reach the decoder
    # differently. The body is asserted too: a 401 that isn't the usual
    # JSON would still break a polling client.
    for _label, _bad in (("latin-1", "Bearer tokén"),
                         ("beyond latin-1", "Bearer tok…en")):
        _r = c.get("/api/messages/open", headers={"Authorization": _bad})
        check(f"non-ASCII token ({_label}) -> 401 not 500",
              _r.status_code == 401, f"got {_r.status_code}")
        check(f"non-ASCII token ({_label}) -> JSON error body",
              _r.status_code == 401
              and json.loads(_r.get_data(as_text=True)) == {"error": "Unauthorized."},
              _r.get_data(as_text=True)[:80])
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

    print("\npush a draft (POST /api/messages/<id>/draft):")
    auth = {"Authorization": f"Bearer {TOKEN}"}
    draft_path = "/api/messages/1/draft"
    PUSHED = "Hi Bobby -- check-in is 4pm. See you then!"

    check("no header -> 401",
          c.post(draft_path, json={"draft_reply": PUSHED}).status_code == 401)
    check("wrong token -> 401",
          c.post(draft_path, json={"draft_reply": PUSHED},
                 headers={"Authorization": "Bearer wrong"}).status_code == 401)
    # No csrf_token is ever sent below. The /api branch of _require_login must
    # return before the form-CSRF check, or every push is a 400.
    check("bearer POST is exempt from form CSRF",
          c.post(draft_path, json={"draft_reply": PUSHED},
                 headers=auth).status_code == 200)

    # Round trip: the GET must now hand back exactly what was pushed. This is
    # the whole point of the endpoint -- the text approved in chat is the text
    # the host sends.
    after = json.loads(c.get("/api/messages/open", headers=auth).get_data(as_text=True))
    check("pushed draft comes back from the GET",
          after["messages"][0]["draft_reply"] == PUSHED,
          f"got {after['messages'][0]['draft_reply']!r}")
    check("push did not mark the message handled", after["count"] == 1,
          f"count={after['count']}")

    print("\n  rejected input must not touch the stored draft:")
    for name, body in (
        ("empty string -> 400", {"draft_reply": ""}),
        ("whitespace only -> 400", {"draft_reply": "   \n  "}),
        ("missing key -> 400", {"other": "x"}),
        ("non-string -> 400", {"draft_reply": 42}),
        ("null -> 400", {"draft_reply": None}),
        ("over the length cap -> 400",
         {"draft_reply": "x" * (appmod.MAX_PUSHED_DRAFT_CHARS + 1)}),
    ):
        got = c.post(draft_path, json=body, headers=auth).status_code
        check(f"  {name}", got == 400, f"got {got}")
    got = c.post(draft_path, data="not json at all", headers=auth).status_code
    check("  non-JSON body -> 400", got == 400, f"got {got}")
    # The draft the host was about to send must have survived all of that.
    still = json.loads(c.get("/api/messages/open", headers=auth).get_data(as_text=True))
    check("draft survived every rejected push",
          still["messages"][0]["draft_reply"] == PUSHED,
          f"got {still['messages'][0]['draft_reply']!r}")

    print("\n  wrong target:")
    got = c.post("/api/messages/9999/draft", json={"draft_reply": PUSHED},
                 headers=auth).status_code
    check("  unknown event id -> 404", got == 404, f"got {got}")
    got = c.post("/api/messages/abc/draft", json={"draft_reply": PUSHED},
                 headers=auth).status_code
    check("  non-numeric id -> 404 (int converter, not a 500)",
          got == 404, f"got {got}")
    # Event 2 is the seeded handled row. Pushing there would park a draft on a
    # card the host has already finished with.
    r409 = c.post("/api/messages/2/draft", json={"draft_reply": PUSHED}, headers=auth)
    check("  already-handled event -> 409", r409.status_code == 409,
          f"got {r409.status_code}")

    print("\n  response shape:")
    r_ok = c.post(draft_path, json={"draft_reply": f"  {PUSHED}  "}, headers=auth)
    body = json.loads(r_ok.get_data(as_text=True))
    check("  ok: true", body.get("ok") is True)
    check("  event_id echoed", body.get("event_id") == 1)
    check("  draft echoed back stripped", body.get("draft_reply") == PUSHED,
          f"got {body.get('draft_reply')!r}")
    check("  url matches the GET's deep link",
          body.get("url") == "https://example.invalid/messages#msg-1",
          f"got {body.get('url')!r}")

    print("\nno send route is exposed under /api:")
    for path in ("/api/messages/send", "/api/messages/1/send"):
        got = c.post(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        check(f"POST {path} -> 404/405", got in (404, 405), f"got {got}")

    # The draft generator's failure path. Stubbed, never a real API call:
    # the point is that whatever Anthropic says reaches the host instead of
    # being reduced to "400 Client Error: Bad Request".
    print("\nAnthropic failures reach the host:")

    class _FakeResp:
        def __init__(self, status, payload=None, text=""):
            self.status_code = status
            self.ok = 200 <= status < 300
            self.reason = "Bad Request" if status == 400 else "OK"
            self._payload = payload or {}
            self.text = text
            self.request = type("R", (), {"method": "POST",
                                          "url": "https://api.anthropic.com/v1/messages"})()

        def json(self):
            return self._payload

    real_post, real_key = appmod.requests.post, appmod.ANTHROPIC_API_KEY
    appmod.ANTHROPIC_API_KEY = "sk-ant-not-a-real-key"
    event = {"guest": "Bobby Tables", "body": "What time is checkin?",
             "booking_id": None}
    try:
        credit_msg = ('{"type":"error","error":{"type":"invalid_request_error",'
                      '"message":"Your credit balance is too low"}}')
        appmod.requests.post = lambda *a, **k: _FakeResp(400, text=credit_msg)
        try:
            appmod.generate_ai_draft(event)
            check("400 body surfaced in the error", False, "no exception raised")
        except Exception as e:
            check("400 body surfaced in the error", "credit balance is too low" in str(e),
                  f"got {str(e)[:120]!r}")
            check("status and URL still reported", "400" in str(e)
                  and "api.anthropic.com" in str(e), f"got {str(e)[:120]!r}")

        # A 200 carrying no text block must not be stored as an empty draft.
        appmod.requests.post = lambda *a, **k: _FakeResp(
            200, payload={"stop_reason": "refusal", "content": []})
        try:
            appmod.generate_ai_draft(event)
            check("empty 200 raises instead of saving a blank draft", False,
                  "no exception raised")
        except Exception as e:
            check("empty 200 raises instead of saving a blank draft",
                  "refusal" in str(e), f"got {str(e)[:120]!r}")

        # The happy path still returns joined text.
        appmod.requests.post = lambda *a, **k: _FakeResp(
            200, payload={"stop_reason": "end_turn",
                          "content": [{"type": "text", "text": "  Hi Bobby!  "}]})
        check("success returns the stripped text",
              appmod.generate_ai_draft(event) == "Hi Bobby!")
    finally:
        appmod.requests.post, appmod.ANTHROPIC_API_KEY = real_post, real_key

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
