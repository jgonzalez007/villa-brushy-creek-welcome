"""Tests for /doors' Kwikset door-code handling.

    python test_doors.py

Covers the ways a door code can quietly land on the wrong slot:

  1. Migration. Rows from before this change carry slots this app guessed,
     which may point at someone else's code. They must come through
     unconfirmed, so they can't be removed from here.
  2. Slots. Creates send slot 0 and record the slot the lock reports;
     a code whose slot wasn't reported is recorded as unknown.
  3. Remove. Only a lock-confirmed slot may be deleted, and the slot comes
     from our own record, never from the form.
  4. Code rules. A code whose first 4 digits match one already sent to the
     lock is refused before anything reaches Kwikset.
  5. Offline locks are flagged on the page.

No network: the Kwikset client is replaced with a fake that records calls.
"""
import os
import pathlib
import sqlite3
import sys
import tempfile

DB = pathlib.Path(tempfile.mkdtemp()) / "doors.db"
os.environ["DB_PATH"] = str(DB)
os.environ["SECRET_KEY"] = "test-only"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["PUBLIC_BASE_URL"] = "https://example.invalid"
# Importing app starts background threads; keep every integration offline
# (see test_api.py for why each of these matters).
for _var in ("OWNERREZ_USERNAME", "OWNERREZ_TOKEN", "IAQUALINK_USERNAME",
             "IAQUALINK_PASSWORD", "KWIKSET_EMAIL", "KWIKSET_REFRESH_TOKEN",
             "ANTHROPIC_API_KEY", "OPENCLAW_API_TOKEN"):
    os.environ.pop(_var, None)

LOCK = "lock-1"

# The pre-change table shape, with two codes whose slots were guessed.
_old = sqlite3.connect(DB)
_old.executescript("""
    CREATE TABLE kwikset_access_codes (
        device_id TEXT NOT NULL, slot INTEGER NOT NULL, booking_key TEXT,
        guest_name TEXT, code TEXT, schedule_json TEXT, created_at TEXT,
        PRIMARY KEY (device_id, slot)
    );
    INSERT INTO kwikset_access_codes VALUES
        ('lock-1', 5, 'bk-old', 'Old Guest', '1111', 'null', '2026-09-01T00:00:00'),
        ('lock-1', 6, NULL, 'Cleaner', '2222', 'null', '2026-09-02T00:00:00');
""")
_old.commit()
_old.close()

import app as appmod  # noqa: E402  (env and old table must exist first)
import kwikset_client  # noqa: E402
from kwikset_codec import parse_assigned_slot, check_code_rules  # noqa: E402

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
        failures.append(name)


class FakeKwikset:
    """Stands in for KwiksetClient. `next_slot` is what the lock "reports"
    for the next create (None = no report)."""

    def __init__(self):
        self.next_slot = None
        self.online = True
        self.added = []
        self.removed = []

    def list_locks(self):
        return [{
            "device_id": LOCK, "name": "Front Door", "home": "Villa",
            "online": self.online, "connectivity": "connected" if self.online else "disconnected",
            "last_updated": None,
        }]

    def add_access_code(self, device_id, name, code, schedule=None):
        self.added.append((device_id, name, code))
        return {"slot": self.next_slot, "name": name[:14], "code": code, "schedule": schedule}

    def remove_access_code(self, device_id, slot):
        self.removed.append((device_id, slot))
        return {"slot": slot}


def rows():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    out = [dict(r) for r in db.execute("SELECT * FROM kwikset_access_codes ORDER BY id")]
    db.close()
    return out


def row_for(code):
    return next((r for r in rows() if r["code"] == code), None)


def post(c, path, data):
    with c.session_transaction() as s:
        s["csrf_token"] = "t"
    return c.post(path, data={**data, "csrf_token": "t"})


def manual_add(c, name, code):
    return post(c, "/doors/manual_add",
                {"device_id": LOCK, "name": name, "code": code, "never_expire": "on"})


def test_codec():
    print("\ncodec:")
    # Literal replies from a HALO-01 during hardware testing.
    check("0301 reply parses to the slot", parse_assigned_slot("030104") == 4)
    check("hex slot parses", parse_assigned_slot("03010A") == 10)
    check("delete reply has no slot", parse_assigned_slot("") is None)
    check("None has no slot", parse_assigned_slot(None) is None)
    check("fresh prefix allowed", check_code_rules("2468", ["1357"]) is None)
    check("duplicate refused", check_code_rules("2468", ["2468"]) is not None)
    check("shared 4-digit prefix refused", check_code_rules("246801", ["2468"]) is not None)
    check("999999 prefix refused", check_code_rules("99999912", []) is not None)


def test_client_sends_slot_zero():
    print("\nclient:")
    sent = {}

    class Recorder(kwikset_client.KwiksetClient):
        def _find_device(self, device_id):
            return {"deviceid": device_id, "deviceconnectivitystatus": "connected"}, {}

        def _api_request(self, path, method="GET", body=None):
            if method == "POST":
                sent["message"] = body["message"]
                return {"data": [{"token": "tok"}]}
            return {"message": "030107", "lastupdatestatus": 1}

    result = Recorder("id").add_access_code(LOCK, "Guest", "4827")
    import base64
    payload = base64.b64decode(sent["message"])
    # [type][len][index][flags]... -- the index byte must be 0.
    check("create sends index 0", payload[2] == 0, f"payload={payload.hex()}")
    check("returns the lock-reported slot", result["slot"] == 7, f"got {result['slot']}")

    class Offline(Recorder):
        def _find_device(self, device_id):
            return {"deviceid": device_id, "deviceconnectivitystatus": "disconnected"}, {}

    try:
        Offline("id").add_access_code(LOCK, "Guest", "4827")
        check("offline lock refused", False, "no error raised")
    except kwikset_client.ValidationError:
        check("offline lock refused", True)


def main():
    test_codec()
    test_client_sends_slot_zero()

    fake = FakeKwikset()
    appmod.get_kwikset_client = lambda: fake
    # Treat Kwikset as connected without a stored refresh token.
    appmod.KWIKSET_EMAIL = "test@example.invalid"
    appmod.fetch_bookings_for_month = lambda year, month: []

    c = appmod.app.test_client()
    c.post("/setup", data={"password": "testpass123", "confirm": "testpass123"})
    c.post("/login", data={"username": "admin", "password": "testpass123"})
    if c.get("/doors").status_code != 200:
        print("\nlogin failed -- can't exercise /doors (see test_api.py on scrypt)")
        return 1

    print("\nmigration:")
    old = row_for("1111")
    check("old rows carried over", old is not None and row_for("2222") is not None)
    check("old guessed slot kept for reference", old and old["slot"] == 5)
    check("old rows unconfirmed", all(not r["slot_confirmed"] for r in rows()))

    print("\nslots:")
    fake.next_slot = 3
    r = manual_add(c, "Pool Guy", "3579")
    new = row_for("3579")
    check("add redirects", r.status_code == 302, f"got {r.status_code}: {r.get_data(as_text=True)}")
    check("lock-reported slot recorded", new and new["slot"] == 3 and new["slot_confirmed"] == 1)

    fake.next_slot = None
    manual_add(c, "Handyman", "4680")
    unknown = row_for("4680")
    check("no report -> slot unknown", unknown and unknown["slot"] is None
          and unknown["slot_confirmed"] == 0)

    print("\ncode rules:")
    before = len(fake.added)
    r = manual_add(c, "Clash", "357912")
    check("shared prefix refused", r.status_code == 500 and "first 4 digits" in r.get_data(as_text=True),
          f"got {r.status_code}")
    check("refused code never reached Kwikset", len(fake.added) == before)
    r = manual_add(c, "Clash", "1111")
    check("clash with a migrated code refused", r.status_code == 500)

    print("\nremove:")
    r = post(c, "/doors/remove", {"code_id": old["id"]})
    check("unconfirmed slot refused", r.status_code == 400, f"got {r.status_code}")
    check("nothing sent for unconfirmed slot", fake.removed == [])

    # A forged slot in the form must be ignored -- the record decides.
    r = post(c, "/doors/remove", {"code_id": new["id"], "slot": "9", "device_id": "other"})
    check("confirmed slot removed", r.status_code == 302, f"got {r.status_code}")
    check("delete used the recorded slot", fake.removed == [(LOCK, 3)], f"got {fake.removed}")
    check("record dropped after remove", row_for("3579") is None)

    r = post(c, "/doors/forget", {"code_id": old["id"]})
    check("forget drops the record", r.status_code == 302 and row_for("1111") is None)
    check("forget never touches the lock", fake.removed == [(LOCK, 3)])

    print("\nslot reuse:")
    fake.next_slot = 4
    manual_add(c, "First", "5791")
    # The lock only hands out a free slot, so a second report of slot 4
    # means the first code was deleted elsewhere.
    manual_add(c, "Second", "6802")
    slot4 = [r for r in rows() if r["slot"] == 4 and r["slot_confirmed"]]
    check("reused slot replaces the stale record",
          len(slot4) == 1 and slot4[0]["code"] == "6802", f"got {slot4}")

    print("\npage:")
    page = c.get("/doors").get_data(as_text=True)
    check("unknown slot shown", ">unknown<" in page)
    check("Forget offered for unconfirmed code", "/doors/forget" in page)
    check("Remove offered for confirmed code", "/doors/remove" in page)
    fake.online = False
    page = c.get("/doors").get_data(as_text=True)
    check("offline lock flagged in picker", "— offline" in page)
    check("offline banner shown", "is offline" in page)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
