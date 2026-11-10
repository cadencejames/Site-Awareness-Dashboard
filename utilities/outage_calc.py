"""
outage_calc.py - turns Orion Down/Up events into site outages and availability.

Pure functions: no database, no network, no clock. Everything it needs comes in
as arguments, so the same events always give the same answer.

How an outage is decided
  1. Each Orion node becomes a list of "down" intervals: a Down event opens one,
     the next Up closes it. An Up with nothing open is ignored (Orion also logs
     Up when a node recovers from Warning). A Down while one is already open is
     ignored (the first Down is the real start). A node still down at the end is
     "ongoing".
  2. A SAD device with several Orion nodes is down only while all of them are.
  3. A SITE is down while its seed device (the CDP seed) is down. A site with
     more than one seed is down only while all its seeds are.
  4. Down periods separated by less than the "up interval" are merged into one
     outage, and only then is the minimum applied: an outage counts if its whole
     merged length is at least the minimum (exactly the minimum counts).
  5. Every other device at a site gets its own outages, merged the same way,
     trimmed to the times the site itself was up, then held to the device minimum.
     They are reported but do not change site availability.

Core hours (Eastern) split every outage into core and outside-core minutes, and
planned windows split those again, giving four buckets:
core_unplanned, core_planned, outside_unplanned, outside_planned.

Availability is measured over core hours only, and is given two ways:
  availability_all           = 1 - core_down / core_total
  availability_excl_planned  = 1 - core_unplanned / (core_total - core_planned)
Planned downtime leaves both the numerator and the denominator in the second.

All times going in and out are Eastern wall-clock datetimes (naive). Inside, the
module works in UTC (through zoneinfo) so that daylight saving cannot distort a duration.
"""
from datetime import datetime, timedelta, timezone

import orion_parser as op

# ---------------------------------------------------------------------------
# Eastern time. Orion's event times are plain local (Eastern) date-times with no
# offset, so the hour that repeats when daylight saving ends is ambiguous.
# These helpers convert to a steady UTC clock for arithmetic and back for
# display. "utc" values are naive datetimes meaning UTC; "local" values are naive
# datetimes meaning Eastern wall-clock time. Needs zoneinfo, which on Windows
# needs the tzdata package (pip install tzdata).
# ---------------------------------------------------------------------------

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception as exc:        # missing zoneinfo module, or no tzdata on this machine
    raise ImportError(
        "outage_calc needs Python's zoneinfo with time zone data for America/New_York. "
        f"On Windows run: pip install tzdata   ({exc})") from exc

UTC = timezone.utc


def is_ambiguous(local):
    """True for a local time that happens twice (the hour when daylight time ends)."""
    first = local.replace(tzinfo=EASTERN, fold=0).utcoffset()
    second = local.replace(tzinfo=EASTERN, fold=1).utcoffset()
    return first != second


def to_utc(local, fold=0):
    """Eastern wall-clock time to naive UTC. `fold` only matters in the repeated
    hour: 0 is the first pass (daylight time), 1 is the second pass (standard time)."""
    return local.replace(tzinfo=EASTERN, fold=1 if fold else 0).astimezone(UTC).replace(tzinfo=None)


def from_utc(utc):
    """Naive UTC to (Eastern wall-clock time, fold). fold is 1 only in the second
    pass of the repeated hour."""
    aware = utc.replace(tzinfo=UTC).astimezone(EASTERN)
    return aware.replace(tzinfo=None), aware.fold


def local_from_utc(utc):
    """Just the wall-clock time from from_utc()."""
    return from_utc(utc)[0]


def assign_utc(events):
    """UTC time for each event, resolving the repeated hour by event order.

    `events` is a list of dicts with a local `time` and an `event_id`. EventIDs
    grow with real time, so within the repeated hour the first pass comes first
    and the second pass starts when the clock reading steps backward. Returns a
    list of UTC datetimes in the same order as `events`, and the number of events
    that fell in a repeated hour where the order could not settle which pass they
    belong to (they are treated as the first pass)."""
    order = sorted(range(len(events)), key=lambda i: (events[i].get("event_id") or 0, events[i]["time"]))
    folds = [0] * len(events)
    state = {}
    inside = 0
    saw_step_back = set()
    for i in order:
        t = events[i]["time"]
        if not is_ambiguous(t):
            continue
        inside += 1
        last = state.get(t.year)
        fold = 0
        if last is not None:
            last_time, last_fold = last
            fold = last_fold
            if last_fold == 0 and t < last_time:
                fold = 1
                saw_step_back.add(t.year)
        state[t.year] = (t, fold)
        folds[i] = fold
    unresolved = 0
    if inside:
        years = {events[i]["time"].year for i in order if is_ambiguous(events[i]["time"])}
        for year in years:
            if year not in saw_step_back:
                unresolved += sum(1 for e in events if is_ambiguous(e["time"]) and e["time"].year == year)
    return [to_utc(e["time"], folds[i]) for i, e in enumerate(events)], unresolved


def month_bounds_utc(year, month):
    """(start, end) of an Eastern calendar month as UTC datetimes; end is exclusive."""
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return to_utc(start), to_utc(end)


DEFAULTS = {
    "min_down_minutes": 1,          # an outage counts if it lasts at least this long
    "up_interval_minutes": 1,       # down periods closer together than this are one outage
    "device_min_down_minutes": None, # None means "same as min_down_minutes"
    "core_days": (0, 1, 2, 3, 4, 5, 6),    # Monday=0 ... Sunday=6
    "core_start_minute": 1 * 60,     # minutes after midnight, Eastern
    "core_end_minute": 23 * 60,
    "before_data": "exclude",        # a node whose first event is an Up: "exclude" its unknown-length outage or "clip" it to data_start
}


# ---------------------------------------------------------------------------
# interval helpers; an interval is (start, end) with start < end
# ---------------------------------------------------------------------------

def _norm(intervals):
    """Sorted, with overlapping or touching intervals joined and empty ones dropped."""
    out = []
    for s, e in sorted(i for i in intervals if i[0] < i[1]):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _intersect(a, b):
    a, b = _norm(a), _norm(b)
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _subtract(a, b):
    out = []
    b = _norm(b)
    for s, e in _norm(a):
        cur = s
        for bs, be in b:
            if be <= cur or bs >= e:
                continue
            if bs > cur:
                out.append((cur, bs))
            cur = max(cur, be)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return out


def _minutes(intervals):
    return sum((e - s).total_seconds() for s, e in intervals) / 60.0


def _group(intervals, gap_minutes):
    """Join intervals whose gap is shorter than gap_minutes. Returns a list of
    {"start", "end", "parts": [(s, e), ...]} in time order."""
    gap = timedelta(minutes=gap_minutes)
    groups = []
    for s, e in sorted(i for i in intervals if i[0] < i[1]):
        if groups and s - groups[-1]["end"] < gap:
            groups[-1]["parts"].append((s, e))
            if e > groups[-1]["end"]:
                groups[-1]["end"] = e
        else:
            groups.append({"start": s, "end": e, "parts": [(s, e)]})
    return groups


def _clip_parts(parts, piece):
    out = []
    for s, e in parts:
        s2, e2 = max(s, piece[0]), min(e, piece[1])
        if s2 < e2:
            out.append((s2, e2))
    return out


# ---------------------------------------------------------------------------
# nodes -> down intervals
# ---------------------------------------------------------------------------

def node_down_intervals(node_events, now_utc, before_data="exclude", data_start_utc=None):
    """Down intervals for one node.

    node_events: list of (utc, event_id, type). Returns (intervals, stats) where
    intervals is [(start, end)] (an ongoing outage ends at now_utc) and stats
    counts stray_up, repeated_down and before_data (a list of the UTC times at
    which a node's first event was an Up that had no Down before it)."""
    stats = {"stray_up": 0, "repeated_down": 0, "before_data": []}
    out, start, first = [], None, True
    for t, _eid, etype in sorted(node_events, key=lambda x: (x[0], x[1] or 0)):
        if etype == op.EVENT_DOWN:
            if start is None:
                start = t
            else:
                stats["repeated_down"] += 1
        elif etype == op.EVENT_UP:
            if start is not None:
                out.append((start, t))
                start = None
            elif first:
                stats["before_data"].append(t)
                if before_data == "clip" and data_start_utc is not None and data_start_utc < t:
                    out.append((data_start_utc, t))
            else:
                stats["stray_up"] += 1
        first = False
    if start is not None and start < now_utc:
        out.append((start, now_utc))
    return _norm(out), stats


# ---------------------------------------------------------------------------
# core hours and planned windows
# ---------------------------------------------------------------------------

def core_windows(span_start_utc, span_end_utc, settings):
    """Core-hours windows (UTC intervals) that touch the span."""
    first = local_from_utc(span_start_utc).date() - timedelta(days=1)
    last = local_from_utc(span_end_utc).date() + timedelta(days=1)
    days = set(settings["core_days"])
    out, day = [], first
    while day <= last:
        if day.weekday() in days:
            midnight = datetime(day.year, day.month, day.day)
            out.append((to_utc(midnight + timedelta(minutes=settings["core_start_minute"])),
                        to_utc(midnight + timedelta(minutes=settings["core_end_minute"]))))
        day += timedelta(days=1)
    return _norm(out)


def _planned_for_site(planned, octet):
    """[(id, utc_start, utc_end)] of the live planned windows that cover this site.

    A planned window is a dict with id, start and end (Eastern), sites (a list of
    site octets, or None for every site), and optionally deleted."""
    out = []
    for w in planned or []:
        if w.get("deleted"):
            continue
        sites = w.get("sites")
        if sites is not None and str(octet) not in {str(s) for s in sites}:
            continue
        s, e = to_utc(w["start"], w.get("start_fold", 0)), to_utc(w["end"], w.get("end_fold", 0))
        if s < e:
            out.append((w.get("id"), s, e))
    return out


def _buckets(pieces, cov, core, planned):
    """Minutes in each of the four buckets for the pieces, counted only inside `cov`."""
    seg = _intersect(pieces, [cov]) if cov else []
    core_seg = _intersect(seg, core)
    plan_seg = _intersect(seg, planned)
    total, core_total = _minutes(seg), _minutes(core_seg)
    plan_total = _minutes(plan_seg)
    core_plan = _minutes(_intersect(core_seg, planned))
    return {"core_unplanned": core_total - core_plan, "core_planned": core_plan,
            "outside_unplanned": (total - core_total) - (plan_total - core_plan),
            "outside_planned": plan_total - core_plan, "total": total}


def _add_buckets(a, b):
    return {k: a.get(k, 0.0) + b.get(k, 0.0) for k in ("core_unplanned", "core_planned", "outside_unplanned", "outside_planned", "total")}


def _round_buckets(b):
    return {k: round(v, 2) for k, v in b.items()}


# ---------------------------------------------------------------------------
# the calculation
# ---------------------------------------------------------------------------

def _record(kind, octet, dev, parts, piece, now_utc, cov, core, planned, planned_ids):
    s, e = piece
    gaps = [round((parts[i + 1][0] - parts[i][1]).total_seconds() / 60.0, 2) for i in range(len(parts) - 1)]
    raw = _buckets([piece], cov, core, planned)
    rec = {
        "kind": kind,
        "site_octet": octet,
        "device_id": dev["device_id"] if dev else None,
        "hostname": dev["hostname"] if dev else None,
        "start": local_from_utc(s),
        "end": None if e >= now_utc else local_from_utc(e),
        "ongoing": e >= now_utc,
        "minutes": round((e - s).total_seconds() / 60.0, 2),
        "parts": [{"start": local_from_utc(ps), "end": None if pe >= now_utc else local_from_utc(pe),
                   "minutes": round((pe - ps).total_seconds() / 60.0, 2)} for ps, pe in parts],
        "up_gaps": gaps,
        "planned_ids": sorted({pid for pid, ps, pe in planned_ids if ps < e and pe > s}, key=lambda x: (x is None, str(x))),
    }
    rec.update(_round_buckets(raw))
    return rec, raw


def calculate(events, devices, match, window_start, window_end, now, settings=None, planned=None, data_start=None):
    """Outages and availability for every SAD site over a window.

    events        normalised Orion events (node_id, time, type, event_id), local times
    devices       SAD devices from orion_match.load_sad_devices()
    match         the dict returned by orion_match.match_nodes()
    window_start, window_end   the period to report, Eastern (window_end exclusive)
    now           the Orion server's current time, Eastern; ends ongoing outages
    settings      overrides for DEFAULTS
    planned       planned windows (see _planned_for_site)
    data_start    earliest moment the event history covers, Eastern. Time before it is
                  not counted. Defaults to the earliest event given.

    Returns {"sites": {octet: {...}}, "notes": {...}, "settings": {...}}."""
    cfg = dict(DEFAULTS)
    cfg.update(settings or {})
    min_down = cfg["min_down_minutes"]
    dev_min = cfg["device_min_down_minutes"] if cfg["device_min_down_minutes"] is not None else min_down
    up_gap = cfg["up_interval_minutes"]

    now_utc = to_utc(now)
    utc_times, unresolved_hour = assign_utc(events)
    if data_start is not None:
        data_start_utc = to_utc(data_start)
    elif utc_times:
        data_start_utc = min(utc_times)
    else:
        data_start_utc = None
    ws, we = to_utc(window_start), to_utc(window_end)
    cov_from = max(ws, data_start_utc) if data_start_utc else ws
    cov_to = min(we, now_utc)
    cov = (cov_from, cov_to) if cov_from < cov_to else None
    core = core_windows(cov_from, max(cov_to, cov_from), cfg) if cov else []
    core_total = _minutes(_intersect([cov], core)) if cov else 0.0

    # events grouped by node, only for nodes that are SAD devices
    matched = match["matches"]
    by_node = {}
    for ev, utc in zip(events, utc_times):
        if ev["node_id"] in matched and ev["type"] in (op.EVENT_DOWN, op.EVENT_UP):
            by_node.setdefault(ev["node_id"], []).append((utc, ev.get("event_id"), ev["type"]))

    notes = {"stray_up": 0, "repeated_down": 0, "before_data": [], "ambiguous_hour_events": unresolved_hour,
             "multi_node_devices": len(match.get("multi_node_devices", ()))}
    node_iv = {}
    for node_id, evs in by_node.items():
        ivs, st = node_down_intervals(evs, now_utc, cfg["before_data"], data_start_utc)
        node_iv[node_id] = ivs
        notes["stray_up"] += st["stray_up"]
        notes["repeated_down"] += st["repeated_down"]
        for t in st["before_data"]:
            notes["before_data"].append({"device_id": matched[node_id]["device_id"], "up_at": local_from_utc(t)})

    # a device is down only while all of its nodes are
    dev_iv = {}
    for dev_id, node_ids in match["node_ids_by_device"].items():
        lists = [node_iv.get(n, []) for n in node_ids]
        cur = lists[0]
        for nxt in lists[1:]:
            cur = _intersect(cur, nxt)
        dev_iv[dev_id] = cur

    by_site = {}
    for d in devices:
        by_site.setdefault(d["site_octet"], []).append(d)

    sites = {}
    for octet in sorted(by_site, key=lambda o: (not o.isdigit(), int(o) if o.isdigit() else 0, o)):
        members = by_site[octet]
        seeds = [d for d in members if d["is_seed"] and d["device_id"] in match["node_ids_by_device"]]
        entry = {"site_octet": octet, "site_name": members[0]["site_name"], "tracked": bool(seeds), "reason": None,
                 "seed_device_ids": [d["device_id"] for d in seeds], "outages": [], "device_outages": [],
                 "core_minutes": round(core_total, 2), "down": _round_buckets(_add_buckets({}, {})),
                 "availability_all": None, "availability_excl_planned": None,
                 "covered_from": local_from_utc(cov[0]) if cov else None,
                 "covered_to": local_from_utc(cov[1]) if cov else None}
        sites[octet] = entry
        if not seeds:
            flagged = any(d["is_seed"] for d in members)
            entry["reason"] = "seed device has no Orion node" if flagged else "no seed device flagged in SAD"
            continue

        planned_list = _planned_for_site(planned, octet)
        planned_iv = _norm([(s, e) for _id, s, e in planned_list])

        site_raw = dev_iv.get(seeds[0]["device_id"], [])
        for seed in seeds[1:]:
            site_raw = _intersect(site_raw, dev_iv.get(seed["device_id"], []))
        site_outages = []                  # (start, end) of those that pass the minimum, for trimming devices
        total = {}
        for g in _group(site_raw, up_gap):
            if (g["end"] - g["start"]).total_seconds() / 60.0 < min_down:
                continue
            piece = (g["start"], g["end"])
            site_outages.append(piece)
            rec, raw = _record("site", octet, None, g["parts"], piece, now_utc, cov, core, planned_iv, planned_list)
            if raw["total"] > 0 or (cov and piece[0] < cov[1] and piece[1] > cov[0]):
                entry["outages"].append(rec)
                total = _add_buckets(total, raw)
        entry["down"] = _round_buckets(_add_buckets(total, {}))
        if core_total > 0:
            core_down = total.get("core_unplanned", 0.0) + total.get("core_planned", 0.0)
            entry["availability_all"] = round(100.0 * (1 - core_down / core_total), 4)
            denom = core_total - total.get("core_planned", 0.0)
            if denom > 0:
                entry["availability_excl_planned"] = round(100.0 * (1 - total.get("core_unplanned", 0.0) / denom), 4)

        seed_ids = {d["device_id"] for d in seeds}
        for dev in sorted((d for d in members if d["device_id"] in dev_iv and d["device_id"] not in seed_ids),
                          key=lambda d: (d["hostname"] or "", d["device_id"])):
            for g in _group(dev_iv[dev["device_id"]], up_gap):
                for piece in _subtract([(g["start"], g["end"])], site_outages):
                    if (piece[1] - piece[0]).total_seconds() / 60.0 < dev_min:
                        continue
                    parts = _clip_parts(g["parts"], piece)
                    rec, raw = _record("device", octet, dev, parts, piece, now_utc, cov, core, planned_iv, planned_list)
                    if cov and piece[0] < cov[1] and piece[1] > cov[0]:
                        entry["device_outages"].append(rec)

    return {"sites": sites, "notes": notes, "settings": cfg,
            "coverage": {"from": local_from_utc(cov[0]) if cov else None,
                         "to": local_from_utc(cov[1]) if cov else None, "core_minutes": round(core_total, 2)}}


def describe_parts(rec):
    """Short text for how a merged outage was built, e.g. '10m + 10m, 5m up between'."""
    if len(rec["parts"]) < 2:
        return ""
    pieces = " + ".join(f"{p['minutes']:g}m" for p in rec["parts"])
    gaps = ", ".join(f"{g:g}m" for g in rec["up_gaps"])
    return f"{pieces} down, up {gaps} between"