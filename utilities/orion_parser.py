"""
orion_parser.py - turns raw SolarWinds Orion (SWIS) rows into the plain
dicts the rest of SAD works with.

Pure functions only: no network, no database, no clock. That is what makes
them easy to test against hand-made rows (see test_orion_offline.py) before
anything is wired into SAD. The I/O side lives in orion_client.py.

Times: Orion reports event and node times in the Orion SERVER's local clock
with no reliable offset. Every time here is a naive datetime in that same
clock; any offset suffix is dropped. Converting to another zone or splitting
by core hours is the outage calculator's job, not this module's.
"""
import re
from datetime import datetime

EVENT_DOWN = 1   # Orion.Events EventType: Node Down (verify on your version)
EVENT_UP = 5     # Orion.Events EventType: Node Up
STATUS_DOWN = 2  # Orion.Nodes Status: 2 = Down


class OrionParseError(ValueError):
    """A row or value that could not be understood."""


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------

_TS = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"\s*(?:Z|[+-]\d{2}(?::?\d{2})?)?$"
)


def parse_time(value):
    """Parse an Orion timestamp into a naive datetime.

    Accepts 'YYYY-MM-DDTHH:MM:SS' or a space instead of the T, an optional
    fraction of any length (Orion can send 7 digits; only 6 are kept), and
    an optional Z / +hh:mm / -hhmm suffix, which is dropped (all values
    come from one server clock). A datetime passes through, minus tzinfo.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if value is None:
        raise OrionParseError("empty time value")
    s = str(value).strip()
    m = _TS.match(s)
    if not m:
        raise OrionParseError(f"unrecognised time value: {value!r}")
    year, month, day, hour, minute, second, frac = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    try:
        return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micro)
    except ValueError as exc:
        raise OrionParseError(f"impossible time value {value!r}: {exc}")


def _to_int(value):
    """int for an int-like value (3, '3', 3.0), else None. Booleans are not ints here."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    s = str(value).strip()
    if re.fullmatch(r"-?[0-9]+", s):
        return int(s)
    return None


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "yes", "y")


def _to_str(value):
    return "" if value is None else str(value).strip()


def normalize_ip(value):
    """A clean dotted-quad IPv4 string, or None (blank, IPv6, junk)."""
    if value is None:
        return None
    parts = str(value).strip().split(".")
    if len(parts) != 4:
        return None
    nums = []
    for part in parts:
        if not re.fullmatch(r"[0-9]{1,3}", part):
            return None
        n = int(part)
        if n > 255:
            return None
        nums.append(n)
    return ".".join(str(n) for n in nums)


def second_octet(ip):
    """The second octet of an IPv4 address as an int (the site number in this
    network's addressing scheme), or None if the address isn't usable."""
    clean = normalize_ip(ip)
    if clean is None:
        return None
    return int(clean.split(".")[1])


# ---------------------------------------------------------------------------
# Row normalisers
# ---------------------------------------------------------------------------

def normalize_node(row):
    """One Orion.Nodes row -> a plain dict. Missing optional fields are fine;
    a row without a usable NodeID raises OrionParseError."""
    node_id = _to_int(row.get("NodeID"))
    if node_id is None:
        raise OrionParseError(f"node row without a usable NodeID: {row.get('NodeID')!r}")
    ip_raw = row.get("IPAddress", row.get("IP_Address"))

    def when(key):
        value = row.get(key)
        if value in (None, ""):
            return None
        try:
            return parse_time(value)
        except OrionParseError:
            return None

    return {
        "node_id": node_id,
        "caption": _to_str(row.get("Caption")),
        "dns": _to_str(row.get("DNS")),
        "sysname": _to_str(row.get("SysName")),
        "ip": normalize_ip(ip_raw),
        "ip_raw": _to_str(ip_raw),
        "status": _to_int(row.get("Status")),
        "status_text": _to_str(row.get("StatusDescription")),
        "unmanaged": _to_bool(row.get("Unmanaged")),
        "unmanage_from": when("UnManageFrom"),
        "unmanage_until": when("UnManageUntil"),
        "vendor": _to_str(row.get("Vendor")),
        "machine_type": _to_str(row.get("MachineType")),
        "poll_interval": _to_int(row.get("PollInterval")),
    }


def normalize_event(row):
    """One Orion.Events row -> {event_id, node_id, time, type}. The node id is
    the event's NetworkNode (or NodeID if that is what was selected)."""
    node_id = _to_int(row.get("NetworkNode", row.get("NodeID")))
    if node_id is None:
        raise OrionParseError("event row without a usable node id")
    etype = _to_int(row.get("EventType"))
    if etype is None:
        raise OrionParseError(f"event row without a usable EventType: {row.get('EventType')!r}")
    return {
        "event_id": _to_int(row.get("EventID")),
        "node_id": node_id,
        "time": parse_time(row.get("EventTime")),
        "type": etype,
    }


def normalize_rows(rows, normalizer):
    """Apply a normaliser to every row. Rows that cannot be understood are
    skipped and described in the returned error list, so one odd row never
    hides the other thousands. Returns (items, errors)."""
    items, errors = [], []
    for index, row in enumerate(rows):
        try:
            items.append(normalizer(row))
        except OrionParseError as exc:
            errors.append(f"row {index + 1}: {exc}")
    return items, errors


# ---------------------------------------------------------------------------
# Event sequence sanity check
# ---------------------------------------------------------------------------

def event_sort_key(event):
    eid = event.get("event_id")
    return (event["time"], -1 if eid is None else eid)


def analyze_event_sequences(events):
    """How well do Down/Up events pair up? Replays Down(1)/Up(5) events per
    node in time order and counts the oddities the outage calculator will have
    to decide about:

      first_is_up   nodes whose first event in the window is an Up (they were
                    already down when the window began)
      first_is_down nodes whose first event is a Down
      double_down   a Down right after a Down for the same node
      double_up     an Up right after an Up for the same node
      other_types   events of any other type (counted, not replayed)
    """
    state, first = {}, {}
    result = {"events": 0, "nodes": 0, "first_is_up": 0, "first_is_down": 0,
              "double_down": 0, "double_up": 0, "other_types": 0}
    for event in sorted(events, key=event_sort_key):
        etype = event["type"]
        if etype not in (EVENT_DOWN, EVENT_UP):
            result["other_types"] += 1
            continue
        result["events"] += 1
        nid = event["node_id"]
        previous = state.get(nid)
        if previous is None:
            first[nid] = etype
        elif previous == etype:
            result["double_down" if etype == EVENT_DOWN else "double_up"] += 1
        state[nid] = etype
    result["nodes"] = len(state)
    result["first_is_up"] = sum(1 for t in first.values() if t == EVENT_UP)
    result["first_is_down"] = sum(1 for t in first.values() if t == EVENT_DOWN)
    return result


# ---------------------------------------------------------------------------
# Scrubbing (for sharing probe output)
# ---------------------------------------------------------------------------

class Scrubber:
    """Swaps real names for stable labels (Node-1, Site-2, ...) so probe output
    can be shared. Labels are handed out in the order values are first seen
    and are stable for the rest of the run. With enabled=False every value
    passes through unchanged."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self._maps = {}

    def label(self, kind, value):
        if not self.enabled:
            return str(value)
        mapping = self._maps.setdefault(kind, {})
        if value not in mapping:
            mapping[value] = f"{kind}-{len(mapping) + 1}"
        return mapping[value]