"""
Python port of kwikset-mcp-node's cognito.js (refresh only -- login itself
stays in Node, see README) and kwikset-client.js.

Login/SRP/phone-verification is intentionally NOT ported here -- it's
genuinely complex (Cognito SRP + a bespoke two-round custom challenge) and
already correctly solved by the existing auth-setup.js. Run that once,
store the resulting refresh_token, and everything below only ever does
the simple, well-documented REFRESH_TOKEN_AUTH flow plus REST calls.
"""
import base64
import datetime
import time

import requests

from kwikset_codec import (
    build_create_access_code_payload,
    build_delete_access_code_payload,
    build_date_range_schedule_bytes,
    build_weekly_schedule_bytes,
    DeviceAccessScheduleType,
    LOCK_ASSIGNS_SLOT,
    parse_assigned_slot,
)

COGNITO_USER_POOL_CLIENT_ID = "5eu1cdkjp1itd1fi7b91m6g79s"
COGNITO_REGION = "us-east-1"
API_HOST = "ynk95r1v52.execute-api.us-east-1.amazonaws.com"
API_USER_AGENT = "VillaBrushyCreekWelcome/1.0"
REQUEST_TIMEOUT_S = 15


class KwiksetAuthError(Exception):
    pass


class NotFoundError(Exception):
    pass


class ValidationError(Exception):
    pass


def refresh_cognito_tokens(email: str, refresh_token: str) -> dict:
    """REFRESH_TOKEN_AUTH against Kwikset's Cognito pool. No SRP needed --
    this grant type just exchanges a refresh token for fresh id/access
    tokens. Ported from cognito.js's refresh(); ID token is what
    kwikset-client.js actually sends as the Bearer token."""
    import boto3  # imported lazily, same pattern as the qrcode/iaqualink
    # optional-dependency handling elsewhere in this app

    client = boto3.client("cognito-idp", region_name=COGNITO_REGION)
    try:
        resp = client.initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
            ClientId=COGNITO_USER_POOL_CLIENT_ID,
        )
    except Exception as e:
        raise KwiksetAuthError(
            "Saved Kwikset session could not be refreshed (it may have "
            "expired or been revoked). Re-run auth-setup.js locally to "
            f"re-login and update the stored refresh token. Underlying error: {e}"
        )

    result = resp["AuthenticationResult"]
    return {
        "email": email,
        "id_token": result["IdToken"],
        "access_token": result["AccessToken"],
        # Refresh grants don't always return a new refresh token -- keep
        # the old one if Cognito didn't issue a new one this time.
        "refresh_token": result.get("RefreshToken", refresh_token),
    }


def _first(d, *keys):
    for k in keys:
        v = d.get(k) if d else None
        if v is not None:
            return v
    return None


class KwiksetClient:
    """Synchronous REST client -- ported from kwikset-client.js. Unlike
    the pool integration, no async/websockets are needed here (this app
    only needs REST reads + the two access-code writes)."""

    def __init__(self, id_token: str):
        self.id_token = id_token

    def _api_request(self, path, method="GET", body=None):
        url = f"https://{API_HOST}/{path}"
        headers = {
            "Host": API_HOST,
            "User-Agent": API_USER_AGENT,
            "Authorization": f"Bearer {self.id_token}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = requests.request(
                method, url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_S
            )
        except requests.Timeout:
            raise RuntimeError(f"Kwikset API request timed out for {method} {path}")

        if not resp.ok:
            text = resp.text[:500] if resp.text else ""
            raise RuntimeError(
                f"Kwikset API returned {resp.status_code} {resp.reason} for "
                f"{method} {path}" + (f": {text}" if text else "")
            )
        if resp.status_code == 204:
            return None
        return resp.json()

    def _homes(self):
        data = self._api_request("prod_v1/users/me/homes?top=200")
        return _first(data, "data") or []

    def _devices_for_home(self, home_id):
        data = self._api_request(f"prod_v1/homes/{home_id}/devices")
        return _first(data, "data") or []

    @staticmethod
    def _summarize(device, home):
        connectivity = _first(device, "deviceconnectivitystatus")
        try:
            updated = int(_first(device, "lastupdatedtimestamp", "lastupdatestatus") or 0)
        except (TypeError, ValueError):
            updated = 0
        return {
            # A disconnected lock keeps reporting its last known state and
            # battery, which can be days old -- surface that instead of
            # letting stale values pass as live.
            "connectivity": connectivity,
            "online": connectivity is None or connectivity == "connected",
            "last_updated": (
                datetime.datetime.fromtimestamp(updated, datetime.timezone.utc)
                if updated > 0 else None
            ),
            "device_id": _first(device, "deviceid", "deviceId", "id"),
            "name": _first(device, "devicename", "deviceName", "name"),
            "home": _first(home, "homename", "homeName", "name") if home else None,
            "status": _first(device, "lockstatus", "doorstatus", "status", "state"),
            "battery_percent": _first(device, "batterypercentage", "battery", "batteryPercentage"),
            "model": _first(device, "modelnumber", "modelNumber", "model"),
            "serial_number": _first(device, "serialnumber", "serialNumber"),
        }

    def list_locks(self):
        locks = []
        for home in self._homes():
            home_id = _first(home, "homeid", "homeId", "id")
            for device in self._devices_for_home(home_id):
                locks.append(self._summarize(device, home))
        return locks

    def _find_device(self, device_id):
        for home in self._homes():
            home_id = _first(home, "homeid", "homeId", "id")
            for device in self._devices_for_home(home_id):
                did = _first(device, "deviceid", "deviceId", "id")
                if str(did) == str(device_id):
                    return device, home
        raise NotFoundError(
            f"No lock found with device_id={device_id!r}. Call list_locks to "
            "see valid IDs -- they can change if a lock is removed and "
            "re-added in the Kwikset app."
        )

    @staticmethod
    def _validate_code_value(code):
        import re
        if not re.match(r"^\d{4,8}$", str(code)):
            raise ValidationError(f"code must be 4-8 digits (got {code!r}).")

    def _access_code_request(self, device_id, method, payload: bytes):
        device, _ = self._find_device(device_id)
        did = _first(device, "deviceid", "deviceId", "id")
        connectivity = _first(device, "deviceconnectivitystatus")
        if connectivity is not None and connectivity != "connected":
            raise ValidationError(
                f"This lock reports connectivity {connectivity!r}, so Kwikset's "
                "cloud can't deliver code changes to it. Bring it back online "
                "(check its Wi-Fi in the Kwikset app) and try again."
            )
        message = base64.b64encode(payload).decode("ascii")
        raw = self._api_request(f"prod_v1/devices/{did}/accesscode", method=method, body={"message": message})
        entries = _first(raw, "data") if isinstance(raw, dict) else raw
        entry = (entries or [{}])[0] or {}
        token = _first(entry, "token", "synctoken", "accesscodetoken")
        return did, raw, token

    def _poll_sync_status(self, did, token, attempts=8, interval_s=1.5):
        """Polls the async sync-status endpoint after a create until the
        lock reports the slot it chose. Returns the last raw response."""
        last = None
        if not token:
            return last
        for i in range(attempts):
            if i:
                time.sleep(interval_s)
            try:
                last = self._api_request(f"prod_v1/devices/{did}/accesscode/{token}")
            except RuntimeError as e:
                last = {"error": str(e)}
            if parse_assigned_slot((last or {}).get("message")) is not None:
                break
        return last

    def add_access_code(self, device_id, name, code, schedule=None):
        """Add a keypad access code. `schedule`, if given, is a dict:
        {"type": "date_range", "start": {...}, "end": {...}} with
        year/month/day/hour/minute keys (local wall-clock time). Omit for
        a permanent, always-allowed code.

        The lock picks the slot (lowest free, never an occupied one) and
        reports it back; the returned "slot" is that reported slot, or None
        if the lock didn't report one in time."""
        if not name or not str(name).strip():
            raise ValidationError("name is required.")
        self._validate_code_value(code)

        if schedule is None:
            schedule_type = DeviceAccessScheduleType.ALL_DAY
            schedule_bytes = b""
        elif schedule["type"] == "date_range":
            schedule_type = DeviceAccessScheduleType.DATE_RANGE
            schedule_bytes = build_date_range_schedule_bytes(schedule["start"], schedule["end"])
        elif schedule["type"] == "weekly":
            schedule_type = DeviceAccessScheduleType.WEEKLY
            schedule_bytes = build_weekly_schedule_bytes(schedule["start"], schedule["end"], schedule["days"])
        else:
            raise ValidationError(f"unknown schedule type: {schedule['type']!r}")

        # Kwikset's real UI truncates names to 14 characters -- match that
        # so what we send doesn't silently get cut differently server-side.
        friendly_name = str(name)[:14]

        payload = build_create_access_code_payload(
            index=LOCK_ASSIGNS_SLOT,
            friendly_name=friendly_name,
            enabled=True,
            code=code,
            schedule_type=schedule_type,
            schedule_bytes=schedule_bytes,
        )
        did, raw, token = self._access_code_request(device_id, "POST", payload)
        sync_status = self._poll_sync_status(did, token)
        return {
            "slot": parse_assigned_slot((sync_status or {}).get("message")),
            "name": friendly_name,
            "code": str(code),
            "schedule": schedule,
            "raw_response": raw,
            "sync_status": sync_status,
        }

    def remove_access_code(self, device_id, slot):
        """Deletes whatever code lives in `slot`. Only ever pass a slot the
        lock reported from add_access_code -- a guessed slot erases
        whichever code is actually there. Kwikset's reply to a delete
        carries no confirmation."""
        if not isinstance(slot, int) or not (0 <= slot <= 255):
            raise ValidationError(f"slot must be an integer 0-255, got {slot!r}")
        payload = build_delete_access_code_payload(slot)
        _did, raw, _token = self._access_code_request(device_id, "DELETE", payload)
        return {"slot": slot, "raw_response": raw}
