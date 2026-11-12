"""
outage_report.py - builds a month's outage report from what SAD has stored.

compute_month() reads the stored Orion events, the node-to-device matches, the
SAD devices and site tiers, and runs outage_calc over one Eastern calendar month.
build_sheets() turns that into the sheets of the Excel workbook (or the CSV files):

  Summary         one row per site: outages, minutes in each bucket, availability
  By tier         the same, averaged per tier
  Outage detail   every site outage, with how merged outages were built
  Device outages  outages of other devices at a site (they do not affect availability)
  Settings used   the thresholds, core hours, period covered, and what was left out

Settings come from the "outage" section of settings.json (all optional):

    "outage": {"min_down_minutes": 15, "up_interval_minutes": 15,
               "device_min_down_minutes": null,
               "core_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
               "core_start": "05:00", "core_end": "17:00",
               "before_data": "exclude"}

"Now" for stored data is Orion's clock at the last pull, not this computer's
clock: nothing after the last pull is known, so an outage still open at that
moment ends there and the report says the data is current through that time.
"""
from datetime import datetime, timezone

import db
import orion_match as om
import orion_parser as op
import outage_calc
import outage_db

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
TIER_LABELS = {0: "Tier 0", 1: "Tier 1", 2: "Tier 2", 3: "Tier 3", 4: "Tier 4"}


class ReportError(Exception):
    pass


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _minutes_of_day(text, label):
    try:
        hour, minute = str(text).strip().split(":")
        hour, minute = int(hour), int(minute)
    except ValueError:
        raise ReportError(f"outage.{label} must look like \"05:00\", not {text!r}") from None
    if not (0 <= hour <= 24 and 0 <= minute < 60) or (hour == 24 and minute):
        raise ReportError(f"outage.{label} is not a time of day: {text!r}")
    return hour * 60 + minute


def _whole(section, key, default, minimum):
    value = section.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or value < minimum:
        raise ReportError(f"outage.{key} must be a whole number of at least {minimum}, not {value!r}")
    return int(value)


def parse_settings(section=None):
    """Validate the 'outage' settings and return the dict outage_calc.calculate() takes.
    Raises ReportError with a plain reason."""
    section = section or {}
    cfg = dict(outage_calc.DEFAULTS)
    cfg["min_down_minutes"] = _whole(section, "min_down_minutes", 15, 1)
    cfg["up_interval_minutes"] = _whole(section, "up_interval_minutes", 15, 0)
    cfg["device_min_down_minutes"] = _whole(section, "device_min_down_minutes", None, 1)
    days = section.get("core_days", list(DAY_NAMES[:5]))
    if not isinstance(days, (list, tuple)) or not days:
        raise ReportError("outage.core_days must be a list such as [\"Mon\", \"Tue\"]")
    numbers = set()
    for d in days:
        if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6:
            numbers.add(d)
        elif isinstance(d, str) and d.strip()[:3].title() in DAY_NAMES:
            numbers.add(DAY_NAMES.index(d.strip()[:3].title()))
        else:
            raise ReportError(f"outage.core_days has an unknown day: {d!r}")
    cfg["core_days"] = tuple(sorted(numbers))
    cfg["core_start_minute"] = _minutes_of_day(section.get("core_start", "05:00"), "core_start")
    cfg["core_end_minute"] = _minutes_of_day(section.get("core_end", "17:00"), "core_end")
    if cfg["core_start_minute"] >= cfg["core_end_minute"]:
        raise ReportError("outage.core_start must be earlier than outage.core_end")
    mode = section.get("before_data", "exclude")
    if mode not in ("exclude", "clip"):
        raise ReportError(f"outage.before_data must be \"exclude\" or \"clip\", not {mode!r}")
    cfg["before_data"] = mode
    return cfg


def load_settings():
    """parse_settings() applied to settings.json's 'outage' section."""
    try:
        import settings_loader
        section = settings_loader.get_section("outage")
    except Exception as exc:
        raise ReportError(str(exc)) from None
    return parse_settings(section)


# ---------------------------------------------------------------------------
# computing a month
# ---------------------------------------------------------------------------

def month_window(year, month):
    """(start, end) of an Eastern calendar month; end is the first moment of the next month."""
    if not (1 <= month <= 12):
        raise ReportError(f"month must be 1 to 12, not {month}")
    start = datetime(year, month, 1)
    return start, (datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1))


def parse_month(text):
    """'2026-11' -> (2026, 11)."""
    try:
        year, month = str(text).strip().split("-")
        year, month = int(year), int(month)
    except ValueError:
        raise ReportError(f"month must look like 2026-11, not {text!r}") from None
    month_window(year, month)
    return year, month


def months_covered(conn):
    """[(year, month)] of every month the stored events touch, oldest first."""
    spans = outage_db.coverage(conn)
    seen = []
    for start, end in spans:
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            if (y, m) not in seen:
                seen.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return seen


def compute_month(conn, year, month, settings=None, planned=None):
    """Run the outage calculation for one month from the stored data.

    Returns a dict with the calculator's result (calc) plus what the sheets need:
    year, month, window, settings, tiers, devices, last_pull, coverage, warnings."""
    pull = outage_db.last_pull(conn)
    if pull is None:
        raise ReportError("nothing has been pulled yet. Run: python pull_orion.py --apply")
    cfg = settings if settings is not None else load_settings()
    start, end = month_window(year, month)
    now = op.parse_time(pull["server_now"])
    coverage = outage_db.coverage(conn)
    devices = om.load_sad_devices(conn)
    calc = outage_calc.calculate(outage_db.load_events(conn), devices, outage_db.load_matches(conn), start, end, now,
                                 settings=cfg, planned=planned, coverage=coverage)
    tiers = {s["site_octet"]: s["tier"] for s in db.get_all_sites(conn, include_unassigned=False)}
    warnings = []
    spans = calc["coverage"]["spans"]
    if not spans:
        warnings.append("The stored events do not cover any part of this month.")
    else:
        if spans[0][0] > start:
            warnings.append(f"Events only start at {spans[0][0]:%Y-%m-%d %H:%M}; earlier days of the month are not counted.")
        if now < end:
            warnings.append(f"The month is not over: data is current through {now:%Y-%m-%d %H:%M} (Orion time).")
        if len(spans) > 1:
            warnings.append(f"{len(spans) - 1} gap(s) inside this month where no pull covered the time; outages in them are unknown.")
    notes = calc["notes"]
    if notes["before_data"]:
        warnings.append(f"{len(notes['before_data'])} outage(s) began before the stored data starts and are left out (length unknown).")
    if notes["ambiguous_hour_events"]:
        warnings.append(f"{notes['ambiguous_hour_events']} event(s) fall in the hour that repeats when daylight saving ends and could not be placed with certainty.")
    return {"year": year, "month": month, "window": (start, end), "settings": cfg, "calc": calc, "tiers": tiers,
            "devices": {d["device_id"]: d for d in devices}, "last_pull": pull, "coverage": coverage, "warnings": warnings}


# ---------------------------------------------------------------------------
# sheets
# ---------------------------------------------------------------------------

def _tier_label(tier):
    return TIER_LABELS.get(tier, "Untiered")


def _site_sort_key(octet):
    return (not octet.isdigit(), int(octet) if octet.isdigit() else 0, octet)


def tier_summary(report):
    """Per-tier figures for tracked sites: a list of dicts in tier order, then 'All tracked sites'."""
    groups = {}
    for octet, entry in report["calc"]["sites"].items():
        if not entry["tracked"]:
            continue
        groups.setdefault(report["tiers"].get(octet), []).append(entry)

    def summarise(label, entries):
        avail = [e["availability_all"] for e in entries if e["availability_all"] is not None]
        avail_ex = [e["availability_excl_planned"] for e in entries if e["availability_excl_planned"] is not None]
        return {"label": label, "sites": len(entries), "sites_without_outage": sum(1 for e in entries if not e["outages"]),
                "avg_all": sum(avail) / len(avail) if avail else None,
                "avg_excl_planned": sum(avail_ex) / len(avail_ex) if avail_ex else None,
                "lowest": min(avail) if avail else None,
                "outages": sum(len(e["outages"]) for e in entries),
                "core_unplanned": sum(e["down"]["core_unplanned"] for e in entries),
                "down_total": sum(e["down"]["total"] for e in entries)}

    out = []
    for tier in sorted((t for t in groups if t is not None)):
        out.append(summarise(_tier_label(tier), groups[tier]))
    if None in groups:
        out.append(summarise("Untiered", groups[None]))
    out.append(summarise("All tracked sites", [e for entries in groups.values() for e in entries]))
    return out


def build_sheets(report):
    """The workbook's sheets as dicts for xlsx_writer (rows hold datetimes, numbers and text)."""
    calc, tiers = report["calc"], report["tiers"]
    sites = calc["sites"]
    order = sorted(sites, key=_site_sort_key)
    r1 = lambda v: None if v is None else round(v, 2)

    summary = [["Site", "Site name", "Tier", "Outages", "Ongoing", "Longest outage (min)", "Down in month (min)",
                "Core unplanned (min)", "Core planned (min)", "Outside core unplanned (min)", "Outside core planned (min)",
                "Core minutes in month", "Availability % (all downtime)", "Availability % (planned excluded)", "Note"]]
    for octet in order:
        e = sites[octet]
        tier = tiers.get(octet)
        if not e["tracked"]:
            summary.append([octet, e["site_name"], tier, None, None, None, None, None, None, None, None, None, None, None, f"Not tracked: {e['reason']}"])
            continue
        outs = e["outages"]
        d = e["down"]
        summary.append([octet, e["site_name"], tier, len(outs), sum(1 for o in outs if o["ongoing"]),
                        max((o["minutes"] for o in outs), default=0), d["total"], d["core_unplanned"], d["core_planned"],
                        d["outside_unplanned"], d["outside_planned"], e["core_minutes"],
                        (e["availability_all"], "percent3") if e["availability_all"] is not None else None,
                        (e["availability_excl_planned"], "percent3") if e["availability_excl_planned"] is not None else None, ""])

    by_tier = [["Group", "Sites tracked", "Sites with no outage", "Average availability % (all downtime)",
                "Average availability % (planned excluded)", "Lowest availability %", "Outages", "Core unplanned (min)", "Down total (min)"]]
    for t in tier_summary(report):
        by_tier.append([t["label"], t["sites"], t["sites_without_outage"],
                        (t["avg_all"], "percent3") if t["avg_all"] is not None else None,
                        (t["avg_excl_planned"], "percent3") if t["avg_excl_planned"] is not None else None,
                        (t["lowest"], "percent3") if t["lowest"] is not None else None,
                        t["outages"], r1(t["core_unplanned"]), r1(t["down_total"])])

    detail = [["Site", "Site name", "Tier", "Start", "End", "Length (min)", "Counted in month (min)", "Core unplanned (min)",
               "Core planned (min)", "Outside core unplanned (min)", "Outside core planned (min)", "How it was built", "Planned window ids"]]
    for octet in order:
        e = sites[octet]
        for o in e["outages"]:
            detail.append([octet, e["site_name"], tiers.get(octet), o["start"], "ongoing" if o["ongoing"] else o["end"], o["minutes"],
                           o["total"], o["core_unplanned"], o["core_planned"], o["outside_unplanned"], o["outside_planned"],
                           outage_calc.describe_parts(o), ", ".join(str(x) for x in o["planned_ids"])])

    devs = [["Site", "Site name", "Device", "Start", "End", "Length (min)", "Counted in month (min)", "Core unplanned (min)",
             "Core planned (min)", "Outside core unplanned (min)", "Outside core planned (min)", "How it was built"]]
    for octet in order:
        e = sites[octet]
        for o in e["device_outages"]:
            devs.append([octet, e["site_name"], o["hostname"], o["start"], "ongoing" if o["ongoing"] else o["end"], o["minutes"],
                         o["total"], o["core_unplanned"], o["core_planned"], o["outside_unplanned"], o["outside_planned"],
                         outage_calc.describe_parts(o)])

    cfg, notes, pull = report["settings"], calc["notes"], report["last_pull"]
    start, end = report["window"]
    hhmm = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    spans = calc["coverage"]["spans"]
    settings_rows = [["Setting", "Value"],
                     ["Report month", f"{report['year']}-{report['month']:02d}"],
                     ["Period (Eastern)", f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}"],
                     ["Data counted", "; ".join(f"{a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M}" for a, b in spans) or "none"],
                     ["Data current through (Orion time)", op.parse_time(pull["server_now"])],
                     ["Last pull recorded (UTC)", pull["pulled_at"]],
                     ["Minimum outage (min)", cfg["min_down_minutes"]],
                     ["Up interval: down periods closer than this are one outage (min)", cfg["up_interval_minutes"]],
                     ["Minimum device outage (min)", cfg["device_min_down_minutes"] if cfg["device_min_down_minutes"] is not None else cfg["min_down_minutes"]],
                     ["Core days", ", ".join(DAY_NAMES[d] for d in cfg["core_days"])],
                     ["Core hours (Eastern)", f"{hhmm(cfg['core_start_minute'])} to {hhmm(cfg['core_end_minute'])}"],
                     ["Core minutes in the counted period", calc["coverage"]["core_minutes"]],
                     ["Outage that began before the data starts", "left out" if cfg["before_data"] == "exclude" else "counted from the start of the data"],
                     ["Up events with no Down before them (ignored)", notes["stray_up"]],
                     ["Down events while already down (ignored)", notes["repeated_down"]],
                     ["Outages that began before the data starts", len(notes["before_data"])],
                     ["Planned downtime", "none applied; availability with and without it is the same"],
                     ["Generated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")]]
    for w in report["warnings"]:
        settings_rows.append(["Warning", w])

    return [
        {"name": "Summary", "rows": summary, "widths": [8, 24, 6, 9, 9, 14, 16, 14, 14, 16, 16, 14, 16, 16, 34], "filter": True},
        {"name": "By tier", "rows": by_tier, "widths": [20, 10, 12, 18, 18, 14, 9, 14, 14]},
        {"name": "Outage detail", "rows": detail, "widths": [8, 24, 6, 17, 17, 12, 14, 14, 14, 16, 16, 34, 16], "filter": True},
        {"name": "Device outages", "rows": devs, "widths": [8, 24, 24, 17, 17, 12, 14, 14, 14, 16, 16, 34], "filter": True},
        {"name": "Settings used", "rows": settings_rows, "widths": [58, 60]},
    ]