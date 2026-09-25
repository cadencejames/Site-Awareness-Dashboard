"""
verify_scan.py - Post-scan sanity report for a discovery run (CDP/MAC/
ARP via orchestrator.py). Doesn't touch device credentials or the
network at all - purely reads what's already in sad.db (plus the
write_queue/ spool next to it) and flags anything that looks off, so
you don't have to click through every site in the GUI by hand after a
127-site run.

Usage (run from the project root, same convention as every other
utilities/ script):
    python3 utilities/verify_scan.py
    python3 utilities/verify_scan.py --csv report.csv

What it checks, in order:

  1. The write queue itself. Runs one write_queue.try_drain() pass
     first (safe/idempotent - same thing gui.py's periodic timer does)
     so anything that was sitting there merely because nobody's drained
     it since the scan finished gets applied right now, then reports
     whatever's STILL there afterward - a leftover item at that point
     means something is durably failing to apply (bad data, a
     constraint violation, etc.), not just "hasn't been picked up yet".

  2. Per-site counts and timestamps. For every site: device/link/
     client/ARP-entry counts, and whether last_cdp_discovery/
     last_arp_collection/last_mac_table_collection actually advanced.
     Flags sites where a phase's timestamp moved but produced
     suspiciously little (or zero) data - the kind of thing that's easy
     to miss scrolling past 127 quick console lines during a live run,
     but obvious once everything's laid out site by site.

  3. A totals/summary line so the overall shape of the run (how many
     sites came back clean vs flagged) is visible at a glance.

This is a sanity check, not a guarantee of correctness - a site that
reports "0 links, 1 device" because its seed genuinely has no CDP
neighbors right now looks identical to one where something silently
broke. Treat every flagged site as "worth a two-minute look", not
automatically "broken".
"""
import argparse
import csv
import os
import sys

import db
import write_queue


def _queue_leftover_count() -> int:
    qdir = write_queue._queue_dir()
    return len([
        f for f in os.listdir(qdir)
        if f.endswith(".json") and not f.startswith(".tmp_")
    ])


def build_report(db_path: str = db.DB_PATH) -> dict:
    """Does the actual work - returns a dict with 'drain_outcomes',
    'queue_leftover', and 'rows' (one dict per site) so this can be
    called from a test or another script without going through the CLI
    plumbing below.
    """
    db.init_db(db_path)

    drain_outcomes = write_queue.try_drain(db_path)
    queue_leftover = _queue_leftover_count()

    rows = []
    with db.get_conn(db_path) as conn:
        # Excludes "Unassigned" - it's a holding pen for misattributed
        # devices, not a real, scannable site (it never gets a seed
        # device flagged, so it never legitimately shows a CDP/ARP
        # timestamp) - including it here would flag it as "never
        # CDP-scanned" on every single run, which isn't a real problem
        # to report.
        sites = db.get_all_sites(conn, include_unassigned=False)
        for site in sites:
            site_id = site["id"]
            device_count = conn.execute(
                "SELECT COUNT(*) FROM devices WHERE site_id = ?", (site_id,)
            ).fetchone()[0]
            link_count = conn.execute(
                "SELECT COUNT(*) FROM links WHERE site_id = ?", (site_id,)
            ).fetchone()[0]
            client_count = conn.execute(
                "SELECT COUNT(*) FROM clients WHERE site_id = ?", (site_id,)
            ).fetchone()[0]
            arp_count = conn.execute(
                "SELECT COUNT(*) FROM arp_entries WHERE site_id = ?", (site_id,)
            ).fetchone()[0]

            flags = []
            if site["last_cdp_discovery"] is None:
                flags.append("never CDP-scanned")
            elif device_count == 0:
                # Shouldn't be reachable in practice (discover_site()
                # bails out before mark_site_run() if there's no seed
                # device to even start from) - flagged anyway as a
                # belt-and-suspenders check rather than assumed away.
                flags.append("CDP ran but 0 devices on file")
            elif device_count == 1 and link_count == 0:
                flags.append("only the seed device present - 0 neighbors found")

            if site["last_arp_collection"] is not None and arp_count == 0:
                flags.append("ARP ran but 0 entries recorded")

            if site["last_mac_table_collection"] is not None and client_count == 0:
                flags.append("MAC correlation ran but 0 clients recorded")

            rows.append({
                "site_octet": site["site_octet"],
                "site_name": site["site_name"] or "",
                "devices": device_count,
                "links": link_count,
                "clients": client_count,
                "arp_entries": arp_count,
                "last_cdp_discovery": site["last_cdp_discovery"] or "",
                "last_arp_collection": site["last_arp_collection"] or "",
                "last_mac_table_collection": site["last_mac_table_collection"] or "",
                "flags": flags,
            })

    return {"drain_outcomes": drain_outcomes, "queue_leftover": queue_leftover, "rows": rows}


def print_report(report: dict) -> None:
    drain_outcomes = report["drain_outcomes"]
    queue_leftover = report["queue_leftover"]
    rows = report["rows"]

    print("=== Write queue ===")
    failed = [(rid, o) for rid, o in drain_outcomes.items() if not o["ok"]]
    applied = len(drain_outcomes) - len(failed)
    if applied:
        print(f"  Drained {applied} item(s) just now that hadn't been applied yet.")
    if failed:
        print(f"  {len(failed)} item(s) failed to apply just now:")
        for rid, outcome in failed:
            print(f"    - {rid}: {outcome['error']}")
    if queue_leftover:
        print(f"  WARNING: {queue_leftover} item(s) still sitting in write_queue/ after this pass - "
              f"durably stuck, not just unpicked-up. Worth investigating before trusting the data below.")
    else:
        print("  Queue is empty - everything queued has been applied.")

    print("\n=== Per-site results ===")
    header = f'{"Octet":<8}{"Name":<22}{"Dev":>5}{"Links":>7}{"Clients":>9}{"ARP":>7}   Flags'
    print(header)
    print("-" * len(header))
    flagged_count = 0
    for row in rows:
        flags_str = "; ".join(row["flags"])
        if row["flags"]:
            flagged_count += 1
        print(
            f'{row["site_octet"]:<8}{row["site_name"][:21]:<22}{row["devices"]:>5}{row["links"]:>7}'
            f'{row["clients"]:>9}{row["arp_entries"]:>7}   {flags_str}'
        )

    print(f"\n=== Summary ===")
    print(f"  {len(rows)} site(s) total, {flagged_count} flagged for review.")
    total_devices = sum(r["devices"] for r in rows)
    total_links = sum(r["links"] for r in rows)
    total_clients = sum(r["clients"] for r in rows)
    total_arp = sum(r["arp_entries"] for r in rows)
    print(f"  Totals: {total_devices} device(s), {total_links} link(s), {total_clients} client(s), {total_arp} ARP entrie(s).")


def write_csv(report: dict, csv_path: str) -> None:
    rows = report["rows"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "site_octet", "site_name", "devices", "links", "clients", "arp_entries",
            "last_cdp_discovery", "last_arp_collection", "last_mac_table_collection", "flags",
        ])
        for row in rows:
            writer.writerow([
                row["site_octet"], row["site_name"], row["devices"], row["links"],
                row["clients"], row["arp_entries"], row["last_cdp_discovery"],
                row["last_arp_collection"], row["last_mac_table_collection"],
                "; ".join(row["flags"]),
            ])
    print(f"\nCSV report written to {csv_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", metavar="PATH", help="Also write the per-site table to a CSV file")
    args = parser.parse_args()

    report = build_report()
    print_report(report)
    if args.csv:
        write_csv(report, args.csv)


if __name__ == "__main__":
    main()
