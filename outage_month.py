#!/usr/bin/env python3
"""
outage_month.py - monthly site outage report from the Orion events SAD has stored.

Run from the project root, like the other SAD scripts:

    python outage_month.py                  last pulled month: summary on screen
    python outage_month.py --month 2026-10  a specific month
    python outage_month.py --site 12        also list that site's outages (SAD site octet)
    python outage_month.py --export         write the Excel workbook to outage_reports/
    python outage_month.py --csv            write CSV files instead (one per sheet)
    python outage_month.py --out DIR        put the files somewhere else
    python outage_month.py --months         list the months the stored events touch

Nothing here contacts Orion. Fill the database first with pull_orion.py.
Thresholds and core hours come from the "outage" section of settings.json
(see utilities/outage_report.py); the defaults are a 15 minute minimum, a
15 minute up interval and Monday-Friday 05:00-17:00 Eastern.
"""
import argparse
import os
import sys
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "utilities"))

import db  # noqa: E402
import outage_calc  # noqa: E402
import outage_db  # noqa: E402
import outage_report as rpt  # noqa: E402
import xlsx_writer  # noqa: E402

STAMP = "%Y-%m-%d %H:%M"


def fmt_minutes(minutes):
    secs = int(round(minutes * 60))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    parts = ([f"{d}d"] if d else []) + ([f"{h}h"] if d or h else []) + [f"{rem // 60}m"]
    return " ".join(parts)


def print_report(report, site=None):
    calc = report["calc"]
    year, month = report["year"], report["month"]
    start, end = report["window"]
    cfg = report["settings"]
    print(f"Outage report {year}-{month:02d}   ({start:%Y-%m-%d} to {end - timedelta(days=1):%Y-%m-%d}, Eastern)")
    print(f"Data current through {report['last_pull']['server_now'][:16]} (Orion time). "
          f"Core minutes counted: {calc['coverage']['core_minutes']:g}.")
    print(f"Minimum outage {cfg['min_down_minutes']} min, up interval {cfg['up_interval_minutes']} min.")
    for w in report["warnings"]:
        print(f"  WARNING: {w}")
    tracked = [e for e in calc["sites"].values() if e["tracked"]]
    print(f"Sites tracked: {len(tracked)}; not tracked: {len(calc['sites']) - len(tracked)}")
    print("\nBy tier:")
    print(f"  {'':<18} {'sites':>5} {'no outage':>9} {'avg avail %':>12} {'lowest %':>9} {'outages':>8} {'core down min':>14}")
    for t in rpt.tier_summary(report):
        avg = f"{t['avg_all']:.3f}" if t["avg_all"] is not None else "-"
        low = f"{t['lowest']:.3f}" if t["lowest"] is not None else "-"
        print(f"  {t['label']:<18} {t['sites']:>5} {t['sites_without_outage']:>9} {avg:>12} {low:>9} {t['outages']:>8} {t['core_unplanned']:>14.1f}")
    worst = sorted((e for e in tracked if e["outages"]), key=lambda e: (e["availability_all"] if e["availability_all"] is not None else 101, e["site_octet"]))
    print(f"\nLowest availability ({min(len(worst), 15)} of {len(worst)} sites with an outage):")
    for e in worst[:15]:
        tier = report["tiers"].get(e["site_octet"])
        tier_text = f"tier {tier}" if tier is not None else "no tier"
        print(f"  site {e['site_octet']:<6} {tier_text:<8} outages {len(e['outages']):3d}  down {e['down']['total']:9.1f} min  "
              f"core {e['down']['core_unplanned']:8.1f}  availability {e['availability_all']:.3f}%")
    if site is not None:
        entry = calc["sites"].get(str(site))
        print(f"\nSite {site}:")
        if entry is None:
            print("  That site is not in SAD.")
            return
        if not entry["tracked"]:
            print(f"  Not tracked: {entry['reason']}.")
            return
        print(f"  Availability {entry['availability_all']:.3f}% over {entry['core_minutes']:g} core minutes "
              f"(core down {entry['down']['core_unplanned']:g} min, outside core {entry['down']['outside_unplanned']:g} min).")
        for o in entry["outages"]:
            ends = "ongoing" if o["end"] is None else f"{o['end']:{STAMP}}"
            extra = outage_calc.describe_parts(o)
            print(f"  SITE   {o['start']:{STAMP}} -> {ends}  {fmt_minutes(o['minutes'])}  "
                  f"(in month: core {o['core_unplanned'] + o['core_planned']:g} / outside {o['outside_unplanned'] + o['outside_planned']:g} min)"
                  + (f"  [{extra}]" if extra else ""))
        for o in entry["device_outages"]:
            ends = "ongoing" if o["end"] is None else f"{o['end']:{STAMP}}"
            print(f"  device {o['start']:{STAMP}} -> {ends}  {fmt_minutes(o['minutes'])}  {o['hostname']}")
        if not entry["outages"] and not entry["device_outages"]:
            print("  no outages this month")


def run(args):
    db.init_db()
    try:
        with db.get_conn() as conn:
            if args.months:
                months = rpt.months_covered(conn)
                if not months:
                    print("Nothing has been pulled yet. Run: python pull_orion.py --apply")
                    return 1
                print("Months the stored events touch: " + ", ".join(f"{y}-{m:02d}" for y, m in months))
                return 0
            pull = outage_db.last_pull(conn)
            if pull is None:
                print("Nothing has been pulled yet. Run: python pull_orion.py --apply")
                return 1
            if args.month:
                year, month = rpt.parse_month(args.month)
            else:
                import orion_parser as op
                now = op.parse_time(pull["server_now"])
                year, month = now.year, now.month
            report = rpt.compute_month(conn, year, month)
    except rpt.ReportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print_report(report, args.site)
    if args.export or args.csv:
        out_dir = args.out or os.path.join(HERE, "outage_reports")
        os.makedirs(out_dir, exist_ok=True)
        base = f"outage_report_{year}-{month:02d}"
        sheets = rpt.build_sheets(report)
        if args.csv:
            paths = xlsx_writer.write_csv_files(out_dir, base, sheets)
            print(f"\nWrote {len(paths)} CSV files to {out_dir}")
        else:
            path = os.path.join(out_dir, base + ".xlsx")
            xlsx_writer.write_xlsx(path, sheets)
            print(f"\nWrote {path}")
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(description="Monthly site outage report from the stored Orion events.")
    p.add_argument("--month", help="month to report, like 2026-11 (default: the month of the last pull)")
    p.add_argument("--site", help="also list the outages of this SAD site octet")
    p.add_argument("--export", action="store_true", help="write the Excel workbook")
    p.add_argument("--csv", action="store_true", help="write CSV files instead of a workbook")
    p.add_argument("--out", help="folder for --export / --csv (default: outage_reports/ in the project root)")
    p.add_argument("--months", action="store_true", help="list the months the stored events touch and exit")
    return p.parse_args(argv)


def main(argv=None):
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())