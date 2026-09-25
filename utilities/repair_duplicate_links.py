"""
repair_duplicate_links.py - One-time cleanup for links that got
recorded TWICE for the same physical cable - once from each end's own
CDP walk (device A reports seeing B; device B separately reports
seeing A). db.py's upsert_link() now prevents this going forward (see
its docstring) - this script cleans up rows that were already written
before that fix existed. Safe to run more than once; a second run
should just report nothing found.

A duplicate pair is identified by: same site, device_a/device_b
swapped between the two rows, AND local_intf/remote_intf swapped to
match - i.e. the two rows describe the exact same cable from each end.
A genuine second physical cable between the same two devices always
uses a different port on at least one end, so it's never mistaken for
a duplicate here.

For each duplicate pair found, the row with the LOWER id (recorded
first) is kept; the other is deleted. Nothing else in the database is
touched - no devices, no other links, no ARP data.

Usage (run from the project root):
    python3 utilities/repair_duplicate_links.py         # preview only, no changes made
    python3 utilities/repair_duplicate_links.py --apply  # actually delete the duplicates
"""

import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402


def find_duplicate_pairs(conn):
    """Returns one row per duplicate pair found, each with the id to
    keep, the id to delete, and enough context to preview clearly.
    """
    return conn.execute(
        """
        SELECT l1.id AS keep_id, l2.id AS delete_id,
               s.site_octet AS site_octet, s.site_name AS site_name,
               da.hostname AS a_hostname, db_.hostname AS b_hostname,
               l1.local_intf AS a_side_intf, l1.remote_intf AS b_side_intf
        FROM links l1
        JOIN links l2
          ON l1.site_id = l2.site_id
         AND l1.device_a_id = l2.device_b_id
         AND l1.device_b_id = l2.device_a_id
         AND IFNULL(l1.local_intf, '')  = IFNULL(l2.remote_intf, '')
         AND IFNULL(l1.remote_intf, '') = IFNULL(l2.local_intf, '')
         AND l1.id < l2.id
        JOIN sites s ON l1.site_id = s.id
        JOIN devices da  ON l1.device_a_id = da.id
        JOIN devices db_ ON l1.device_b_id = db_.id
        ORDER BY s.site_octet, da.hostname
        """
    ).fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually delete the duplicate rows (default: preview only)")
    args = parser.parse_args()

    with db.get_conn() as conn:
        pairs = find_duplicate_pairs(conn)

        if not pairs:
            print("No duplicate links found.")
            return

        print(f"Found {len(pairs)} duplicate link pair(s):\n")
        for row in pairs:
            site_label = row["site_name"] or row["site_octet"]
            print(
                f"  site {site_label} ({row['site_octet']}): "
                f"{row['a_hostname']} ({row['a_side_intf']}) <-> {row['b_hostname']} ({row['b_side_intf']})  "
                f"[keeping link id {row['keep_id']}, deleting id {row['delete_id']}]"
            )

        if not args.apply:
            print(f"\nPreview only - no changes made. Re-run with --apply to actually delete these {len(pairs)} duplicate row(s).")
            return

        for row in pairs:
            conn.execute("DELETE FROM links WHERE id = ?", (row["delete_id"],))
        print(f"\nDeleted {len(pairs)} duplicate link row(s).")


if __name__ == "__main__":
    main()
