"""
Python port of kwikset-mcp-node's src/tlv8.js and src/access-code-codec.js.

Every byte format here is ported directly from real source that was itself
confirmed by decompiling the Kwikset Android app (com.kwikset.blewifi)
with jadx -- see the original Node project's "DoorLock" trace doc. This
file changes NOTHING about the wire format; it's a line-for-line port to
Python, not a redesign.
"""
import re


# ---------------------------------------------------------------------------
# tlv8.js port
# ---------------------------------------------------------------------------
class DeviceAccessTlv8SubType:
    ACCESS_CODE = 0
    SCHEDULE = 1
    FRIENDLY_NAME = 2
    PROXY_AUTHORIZATION = 8
    BIOMETRIC_PROFILE = 16


class TxCommand:
    SET_ACCESS_CODE = 4
    DELETE_DEVICE_ACCESS = 5
    DELETE_ALL_ACCESS_CODES = 10
    ACCESS_CODE_READ_SETTINGS = 32
    EDIT_ACCESS_CODE = 113


def tlv8_record(type_: int, data: bytes) -> bytes:
    """[type: 1 byte][length: 1 byte][data: N bytes]. No multi-byte
    length, no padding, no CRC -- matches tlv8.js exactly."""
    if len(data) > 255:
        raise ValueError(f"TLV8 record data too long ({len(data)} bytes, max 255)")
    return bytes([type_ & 0xFF, len(data) & 0xFF]) + data


# ---------------------------------------------------------------------------
# access-code-codec.js port
# ---------------------------------------------------------------------------
_CODE_RE = re.compile(r"^\d{4,8}$")


def encode_access_code_digits(code) -> bytes:
    """Packed BCD, two digits per byte (high nibble first), 0xF padding
    nibble on the last byte when digit count is odd (5 or 7 digits).
    Ported from encodeAccessCodeDigits in access-code-codec.js."""
    s = str(code)
    if not _CODE_RE.match(s):
        raise ValueError(f"access code must be 4-8 digits, got {code!r}")
    d = [ord(c) - 48 for c in s]
    b = [(d[0] << 4) | d[1], (d[2] << 4) | d[3]]
    if len(d) > 4:
        if len(d) == 5:
            b.append((d[4] << 4) | 0xF)
        else:
            b.append((d[4] << 4) | d[5])
            if len(d) == 7:
                b.append((d[6] << 4) | 0xF)
            elif len(d) == 8:
                b.append((d[6] << 4) | d[7])
    return bytes(b)


class DeviceAccessScheduleType:
    ALL_DAY = 1
    DATE_RANGE = 3
    WEEKLY = 4
    ONE_TIME_UNLIMITED = 5
    ONE_TIME_24_HOUR = 6
    PROXY_AUTHORIZATION = 8


SCHEDULE_TYPE_ALL_DAY = DeviceAccessScheduleType.ALL_DAY


def _pack_schedule_time_range(start_hour, start_minute, end_hour, end_minute) -> bytes:
    """3-byte time-of-day pack, ported from packScheduleTimeRange:
    byte0 = startMinute << 2
    byte1 = endMinute | ((startHour & 0x3) << 6)
    byte2 = (startHour >> 2) | ((endHour & 0x1f) << 3)
    """
    sh = start_hour & 0x1F
    sm = start_minute & 0x3F
    eh = end_hour & 0x1F
    em = end_minute & 0x3F
    return bytes([
        (sm << 2) & 0xFF,
        (em | (sh << 6)) & 0xFF,
        ((sh >> 2) | (eh << 3)) & 0xFF,
    ])


def build_date_range_schedule_bytes(start: dict, end: dict) -> bytes:
    """7-byte DateRange schedule sub-record: 3 time-of-day bytes + a
    4-byte little-endian date bitfield. Ported from
    buildDateRangeScheduleBytes in access-code-codec.js.
    start/end dicts need: hour, minute, month, day, year (local wall-clock,
    NOT epoch time -- matches the lock's own timezone)."""
    time_bytes = _pack_schedule_time_range(start["hour"], start["minute"], end["hour"], end["minute"])
    date_bits = (
        (start["month"] & 0xF)
        | ((end["month"] & 0xF) << 4)
        | ((start["day"] & 0x1F) << 8)
        | ((end["day"] & 0x1F) << 13)
        | (((start["year"] - 2000) & 0x7F) << 18)
        | (((end["year"] - 2000) & 0x7F) << 25)
    )
    date_bytes = bytes([
        date_bits & 0xFF,
        (date_bits >> 8) & 0xFF,
        (date_bits >> 16) & 0xFF,
        (date_bits >> 24) & 0xFF,
    ])
    return time_bytes + date_bytes


def build_weekly_schedule_bytes(start: dict, end: dict, days: dict) -> bytes:
    """4-byte Weekly schedule sub-record: 3 time-of-day bytes + a
    day-of-week bitmask (Sun=0x01 ... Sat=0x40). Ported from
    buildWeeklyScheduleBytes. `days` needs boolean keys: sunday, monday,
    tuesday, wednesday, thursday, friday, saturday."""
    time_bytes = _pack_schedule_time_range(start["hour"], start["minute"], end["hour"], end["minute"])
    day_byte = (
        (0x01 if days.get("sunday") else 0)
        | (0x02 if days.get("monday") else 0)
        | (0x04 if days.get("tuesday") else 0)
        | (0x08 if days.get("wednesday") else 0)
        | (0x10 if days.get("thursday") else 0)
        | (0x20 if days.get("friday") else 0)
        | (0x40 if days.get("saturday") else 0)
    )
    return time_bytes + bytes([day_byte])


def build_create_access_code_payload(
    index: int,
    friendly_name: str,
    enabled: bool,
    code,
    schedule_type: int = SCHEDULE_TYPE_ALL_DAY,
    schedule_bytes: bytes = b"",
) -> bytes:
    """Payload for POST devices/{id}/accesscode. Ported from
    buildCreateAccessCodePayload: main AccessCode TLV8 record, optional
    Schedule TLV8 record (only if schedule_bytes is non-empty), then a
    FriendlyName TLV8 record. No outer TxCommand wrapper."""
    if not isinstance(index, int) or not (0 <= index <= 255):
        raise ValueError(f"index must be an integer 0-255, got {index!r}")

    header = bytes([
        index & 0xFF,
        (1 if enabled else 0) | ((schedule_type & 0x0F) << 4),
    ])
    code_bytes = encode_access_code_digits(code)
    main_record = tlv8_record(DeviceAccessTlv8SubType.ACCESS_CODE, header + code_bytes)

    schedule_record = (
        tlv8_record(DeviceAccessTlv8SubType.SCHEDULE, schedule_bytes)
        if len(schedule_bytes) > 0
        else b""
    )
    name_record = tlv8_record(
        DeviceAccessTlv8SubType.FRIENDLY_NAME, str(friendly_name).encode("utf-8")
    )
    return main_record + schedule_record + name_record


def build_delete_access_code_payload(index: int) -> bytes:
    """Payload for DELETE devices/{id}/accesscode. Ported from
    buildDeleteAccessCodePayload: single TLV8 record, type =
    TxCommand.DeleteDeviceAccess (5), data = the 1-byte index."""
    if not isinstance(index, int) or not (0 <= index <= 255):
        raise ValueError(f"index must be an integer 0-255, got {index!r}")
    return tlv8_record(TxCommand.DELETE_DEVICE_ACCESS, bytes([index & 0xFF]))
