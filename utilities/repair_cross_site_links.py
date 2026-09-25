"""
repair_cross_site_links.py - One-time repair for cross-site links
recorded by an earlier version of orchestrator.py, which attributed a
neighbor's site purely from its IP's octet - unreliable for WAN-facing
addresses that don't follow the normal per-site addressing scheme.

Re-evaluates each already-recorded cross-site link's "foreign" device
(the one that doesn't share a site with whatever discovered it) using
the current hostname-first rule, and moves it to its correct site (or
Unassigned) if that differs from where it's currently filed.

Usage (run from the project root):
    python3 utilities/repair_cross_site_links.py

Safe to re-run - devices already correctly attributed are left alone.

If the correct destination already has a device with the same
hostname (e.g. the WAN-discovered duplicate and the real,
inventory-known device are actually the same physical box), this does
NOT attempt to merge them automatically - merging device_ips/links
history safely is a real operation, not a simple move, and getting it
wrong risks losing data. Those cases are reported so you can look at
them by hand.
"""

import sqlite3

import db


def repair(db_path: str = db.DB_PATH) -> None:
    moved = 0
    already_correct = 0
    conflicts = []

    with db.get_conn(db_path) as conn:
        # A cross-site link is one whose two devices don't share a
        # site. l.site_id is the site that discovered/scanned it.
        rows = conn.execute(
            """
            SELECT l.site_id AS scanning_site_id,
                   da.id AS device_a_id, da.site_id AS device_a_site_id, da.hostname AS device_a_hostname,
                   db_.id AS device_b_id, db_.site_id AS device_b_site_id, db_.hostname AS device_b_hostname
            FROM links l
            JOIN devices da ON l.device_a_id = da.id
            JOIN devices db_ ON l.device_b_id = db_.id
            WHERE da.site_id != db_.site_id
            """
        ).fetchall()

        for row in rows:
            if row["device_a_site_id"] != row["scanning_site_id"]:
                foreign_id = row["device_a_id"]
                foreign_hostname = row["device_a_hostname"]
                foreign_site_id = row["device_a_site_id"]
            elif row["device_b_site_id"] != row["scanning_site_id"]:
                foreign_id = row["device_b_id"]
                foreign_hostname = row["device_b_hostname"]
                foreign_site_id = row["device_b_site_id"]
            else:
                continue  # shouldn't happen, but be defensive

            matches = db.find_devices_by_hostname_anywhere(conn, foreign_hostname)
            matches_at_scanning_site = [m for m in matches if m["site_id"] == row["scanning_site_id"]]
            matches_elsewhere = [m for m in matches if m["site_id"] != row["scanning_site_id"] and m["id"] != foreign_id]

            if matches_at_scanning_site:
                correct_site_id = row["scanning_site_id"]
                reason = "matches a device at the scanning site - hostname says it belongs there"
            elif len(matches_elsewhere) == 1:
                correct_site_id = matches_elsewhere[0]["site_id"]
                reason = f"matches an existing device at site {matches_elsewhere[0]['owning_site_octet']}"
            else:
                correct_site_id = db.get_or_create_unassigned_site(conn)
                reason = "ambiguous or unrecognized hostname - filed under Unassigned"

            if correct_site_id == foreign_site_id:
                already_correct += 1
                continue

            try:
                conn.execute("UPDATE devices SET site_id = ? WHERE id = ?", (correct_site_id, foreign_id))
                moved += 1
                print(f"Moved '{foreign_hostname}' -> site id {correct_site_id} ({reason})")
            except sqlite3.IntegrityError:
                conflicts.append((foreign_hostname, foreign_id, correct_site_id))
                print(f"CONFLICT: '{foreign_hostname}' (device id {foreign_id}) should move to site id "
                      f"{correct_site_id}, but a device with that hostname already exists there. "
                      f"Not auto-merged - needs manual review.")

    print(f"\nMoved: {moved}")
    print(f"Already correct: {already_correct}")
    print(f"Conflicts needing manual review: {len(conflicts)}")
    if conflicts:
        for hostname, dev_id, target_site_id in conflicts:
            print(f"  - '{hostname}' (device id {dev_id}) -> target site id {target_site_id}")


if __name__ == "__main__":
    repair()
