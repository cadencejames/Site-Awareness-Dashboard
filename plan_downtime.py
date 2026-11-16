#!/usr/bin/env python3
"""
plan_downtime.py - planned downtime windows for the outage report.

A planned window is a stretch of time when a site (or every site) was expected to
be down on purpose. Time inside a window that a site was really down counts as
"planned" and is left out of the availability that excludes planned downtime.
All times are Eastern, the same clock the Orion events use.

Run from the project root. Nothing is changed unless you add --apply; without it
you see exactly what would happen, including how site availability would move.

    python plan_downtime.py list                      the windows (add --all to include deleted ones)
    python plan_downtime.py outages --site 12         number the site's outages for the month
    python plan_downtime.py add --sites 12,13 --start "2026-11-20 22:00" --end "2026-11-21 02:00" --reason "UPS swap" --apply
    python plan_downtime.py add --all-sites --start "2026-12-06 01:00" --end "2026-12-06 05:00" --apply
    python plan_downtime.py mark --site 12 --number 1 --reason "carrier maintenance" --apply
                                                      turn outage number 1 into a planned window;
                                                      add --start / --end to trim it first
    python plan_downtime.py trim 5 --end "2026-11-21 01:30" --apply
    python plan_downtime.py delete 5 --apply          soft delete; "restore 5" brings it back

Dates look like "2026-11-20 22:00". --month 2026-11 picks the month for outages and
the availability preview (default: the month of the last pull).
"""
import argparse
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "utilities"))

import db  # noqa: E402
import orion_parser as op  # noqa: E402
import outage_db  # noqa: E402
import outage_report as rpt  # noqa: E402
import write_queue  # noqa: E402

STAMP = "%Y-%m-%d %H:%M"


def parse_when(text, label):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    raise SystemExit(f"ERROR: {label} must look like \"2026-11-20 22:00\", not {text!r}")


def month_of(conn, text):
    if text:
        return rpt.parse_month(text)
    pull = outage_db.last_pull(conn)
    if pull is None:
        raise rpt.ReportError("nothing has been pulled yet. Run: python pull_orion.py --apply")
    now = op.parse_time(pull["server_now"])
    return now.year, now.month


def sites_text(sites):
    return "all sites" if sites is None else "site " + ", ".join(sites)


def print_windows(windows):
    if not windows:
        print("No planned windows.")
        return
    print(f"{'ID':>4}  {'sites':<18} {'start':<16} {'end':<16} {'minutes':>8}  reason")
    for w in windows:
        mins = (w["end"] - w["start"]).total_seconds() / 60.0
        flag = "  [deleted]" if w["deleted"] else ""
        where = "all sites" if w["sites"] is None else ",".join(w["sites"])
        if len(where) > 18:
            where = where[:15] + "..."
        how = " (from an outage)" if w["source"] == "from_outage" else ""
        print(f"{w['id']:>4}  {where:<18} {w['start']:{STAMP}} {w['end']:{STAMP}} {mins:>8.0f}  {w['reason'] or ''}{how}{flag}")


def months_spanned(start, end, limit=3):
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month) and len(out) < limit:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def show_impact(conn, before, after, months, only_sites=None):
    """Print how site figures move between two sets of planned windows."""
    rows = []
    for year, month in months:
        a = rpt.compute_month(conn, year, month, planned=before)["calc"]["sites"]
        b = rpt.compute_month(conn, year, month, planned=after)["calc"]["sites"]
        for octet, new in b.items():
            if not new["tracked"] or (only_sites is not None and octet not in only_sites):
                continue
            old = a[octet]
            if (old["down"]["core_planned"], old["down"]["outside_planned"]) == (new["down"]["core_planned"], new["down"]["outside_planned"]):
                continue
            rows.append((year, month, octet, old, new))
    if not rows:
        print("No site outage overlaps this window, so no site's figures change.")
        return
    print("Effect on site outages (planned minutes are left out of the second availability figure):")
    for year, month, octet, old, new in rows[:25]:
        def fig(e):
            return "-" if e["availability_excl_planned"] is None else f"{e['availability_excl_planned']:.3f}%"
        print(f"  {year}-{month:02d} site {octet}: planned core {old['down']['core_planned']:g} -> {new['down']['core_planned']:g} min, "
              f"outside core {old['down']['outside_planned']:g} -> {new['down']['outside_planned']:g} min; "
              f"availability excluding planned {fig(old)} -> {fig(new)} (counting all downtime stays {new['availability_all']:.3f}%)")
    if len(rows) > 25:
        print(f"  ... and {len(rows) - 25} more")


def apply_change(args, conn, action, payload, trial):
    """Dry run by default. `trial(conn)` makes the change inside an open transaction so it is
    fully validated; the transaction is rolled back. With --apply the change is queued."""
    trial(conn)
    conn.rollback()
    if not args.apply:
        print("\nDry run only, nothing was changed. Add --apply to save it.")
        return 0
    try:
        result = write_queue.queue_and_wait(action, timeout=60, **payload)
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: saving failed: {exc}", file=sys.stderr)
        return 1
    print(f"\nSaved{f' as window {result}' if action == 'add_planned_window' and isinstance(result, int) else ''}.")
    return 0


def cmd_list(args, conn):
    print_windows(outage_db.load_planned_windows(conn, include_deleted=args.all))
    return 0


def cmd_outages(args, conn):
    year, month = month_of(conn, args.month)
    report = rpt.compute_month(conn, year, month)
    entry = report["calc"]["sites"].get(str(args.site))
    if entry is None:
        print(f"Site {args.site} is not in SAD.")
        return 1
    if not entry["tracked"]:
        print(f"Site {args.site} is not tracked: {entry['reason']}.")
        return 1
    print(f"Site {args.site} outages in {year}-{month:02d}:")
    if not entry["outages"]:
        print("  none")
    for i, o in enumerate(entry["outages"], start=1):
        end = "ongoing" if o["ongoing"] else f"{o['end']:{STAMP}}"
        planned = f"   [planned window {', '.join(str(x) for x in o['planned_ids'])}]" if o["planned_ids"] else ""
        print(f"  {i}. {o['start']:{STAMP}} -> {end}   {o['minutes']:g} min{planned}")
    return 0


def window_args(args, conn, start, end, reason, sites, source):
    windows = outage_db.load_planned_windows(conn)
    months = months_spanned(start, end)
    scope = sites_text(sites)
    print(f"Planned window: {scope}, {start:{STAMP}} to {end:{STAMP}} ({(end - start).total_seconds() / 60:g} min)"
          + (f", reason: {reason}" if reason else ""))

    def trial(c):
        wid = outage_db.add_planned_window(c, sites, start, end, reason, source)
        after = windows + [w for w in outage_db.load_planned_windows(c) if w["id"] == wid]
        show_impact(c, windows, after, months, None if sites is None else set(sites))
    payload = {"sites": sites, "start": outage_db.fmt_time(start), "end": outage_db.fmt_time(end), "reason": reason, "source": source}
    return apply_change(args, conn, "add_planned_window", payload, trial)


def cmd_add(args, conn):
    if bool(args.sites) == bool(args.all_sites):
        print("ERROR: give either --sites 12,13 or --all-sites.", file=sys.stderr)
        return 1
    start, end = parse_when(args.start, "--start"), parse_when(args.end, "--end")
    sites = None if args.all_sites else [x for x in args.sites.replace(" ", "").split(",") if x]
    try:
        sites, start, end, reason = outage_db.validate_window(sites, start, end, args.reason)
        return window_args(args, conn, start, end, reason, sites, "manual")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_mark(args, conn):
    year, month = month_of(conn, args.month)
    report = rpt.compute_month(conn, year, month)
    entry = report["calc"]["sites"].get(str(args.site))
    if entry is None or not entry["tracked"]:
        print(f"ERROR: site {args.site} is not a tracked site.", file=sys.stderr)
        return 1
    if not 1 <= args.number <= len(entry["outages"]):
        print(f"ERROR: site {args.site} has {len(entry['outages'])} outage(s) in {year}-{month:02d}; "
              f"see them with: python plan_downtime.py outages --site {args.site}", file=sys.stderr)
        return 1
    outage = entry["outages"][args.number - 1]
    start = parse_when(args.start, "--start") if args.start else outage["start"]
    if args.end:
        end = parse_when(args.end, "--end")
    elif outage["ongoing"]:
        print("ERROR: that outage is still going. Give --end for when the planned work finishes.", file=sys.stderr)
        return 1
    else:
        end = outage["end"]
    end_text = "ongoing" if outage["ongoing"] else f"{outage['end']:{STAMP}}"
    print(f"Outage {args.number}: {outage['start']:{STAMP}} -> {end_text}")
    if start >= (outage["end"] or end) or end <= outage["start"]:
        print("WARNING: the window does not overlap that outage.")
    try:
        sites, start, end, reason = outage_db.validate_window([str(args.site)], start, end, args.reason)
        return window_args(args, conn, start, end, reason, sites, "from_outage")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_trim(args, conn):
    windows = outage_db.load_planned_windows(conn, include_deleted=True)
    current = next((w for w in windows if w["id"] == args.id), None)
    if current is None:
        print(f"ERROR: there is no planned window number {args.id}.", file=sys.stderr)
        return 1
    start = parse_when(args.start, "--start") if args.start else None
    end = parse_when(args.end, "--end") if args.end else None
    if start is None and end is None and args.reason is None:
        print("ERROR: give --start, --end or --reason.", file=sys.stderr)
        return 1
    new_start, new_end = start or current["start"], end or current["end"]
    print(f"Window {args.id}: {current['start']:{STAMP}} to {current['end']:{STAMP}}  ->  {new_start:{STAMP}} to {new_end:{STAMP}}")
    live = [w for w in windows if not w["deleted"]]
    months = months_spanned(min(current["start"], new_start), max(current["end"], new_end))
    sites = None if current["sites"] is None else set(current["sites"])

    def trial(c):
        outage_db.update_planned_window(c, args.id, start, end, args.reason)
        after = outage_db.load_planned_windows(c)
        show_impact(c, live, after, months, sites)
    payload = {"window_id": args.id, "start": None if start is None else outage_db.fmt_time(start),
               "end": None if end is None else outage_db.fmt_time(end), "reason": args.reason}
    try:
        return apply_change(args, conn, "update_planned_window", payload, trial)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_delete_restore(args, conn, deleting):
    windows = outage_db.load_planned_windows(conn, include_deleted=True)
    current = next((w for w in windows if w["id"] == args.id), None)
    if current is None:
        print(f"ERROR: there is no planned window number {args.id}.", file=sys.stderr)
        return 1
    print(f"{'Delete' if deleting else 'Restore'} window {args.id}: {sites_text(current['sites'])}, "
          f"{current['start']:{STAMP}} to {current['end']:{STAMP}}")
    live = [w for w in windows if not w["deleted"]]
    months = months_spanned(current["start"], current["end"])
    func = outage_db.delete_planned_window if deleting else outage_db.restore_planned_window

    def trial(c):
        func(c, args.id)
        show_impact(c, live, outage_db.load_planned_windows(c), months, None if current["sites"] is None else set(current["sites"]))
    try:
        return apply_change(args, conn, "delete_planned_window" if deleting else "restore_planned_window", {"window_id": args.id}, trial)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def parse_args(argv):
    p = argparse.ArgumentParser(description="Planned downtime windows for the outage report.")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("list", help="list the planned windows")
    s.add_argument("--all", action="store_true", help="include deleted windows")
    s = sub.add_parser("outages", help="number a site's outages for the month")
    s.add_argument("--site", required=True)
    s.add_argument("--month")
    s = sub.add_parser("add", help="add a window")
    s.add_argument("--sites", help="site octets, like 12,13")
    s.add_argument("--all-sites", action="store_true")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--reason")
    s.add_argument("--apply", action="store_true")
    s = sub.add_parser("mark", help="make a window from one of a site's outages")
    s.add_argument("--site", required=True)
    s.add_argument("--number", type=int, required=True, help="the number shown by the outages command")
    s.add_argument("--month")
    s.add_argument("--start", help="trim the window to start here")
    s.add_argument("--end", help="trim the window to end here (required for an ongoing outage)")
    s.add_argument("--reason")
    s.add_argument("--apply", action="store_true")
    s = sub.add_parser("trim", help="change a window's start, end or reason")
    s.add_argument("id", type=int)
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--reason")
    s.add_argument("--apply", action="store_true")
    for name in ("delete", "restore"):
        s = sub.add_parser(name, help=f"{name} a window")
        s.add_argument("id", type=int)
        s.add_argument("--apply", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    db.init_db()
    try:
        with db.get_conn() as conn:
            if args.command == "list":
                return cmd_list(args, conn)
            if args.command == "outages":
                return cmd_outages(args, conn)
            if args.command == "add":
                return cmd_add(args, conn)
            if args.command == "mark":
                return cmd_mark(args, conn)
            if args.command == "trim":
                return cmd_trim(args, conn)
            return cmd_delete_restore(args, conn, args.command == "delete")
    except rpt.ReportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())