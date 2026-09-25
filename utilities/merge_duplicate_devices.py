"""
merge_duplicate_devices.py - One-time merge tool for devices that turn
out to be duplicates: a real, already-known device (e.g. from
inventory) and a leftover CDP-discovered duplicate that got filed
under a bogus site by the old octet-based cross-site logic (see
repair_cross_site_links.py's docstring for the full backstory).

For every already-recorded cross-site link, this looks at the
"foreign" device (the one that doesn't match the scanning site) and
searches the WHOLE database (not just the scanning site - the real
device can be at any other site entirely, e.g. when the duplicate is
reached via a WAN link to a site unrelated to the one that discovered
it) for another device with that exact hostname. If exactly one is
found, that's the real device, and the foreign one is merged into it:
  - the duplicate's device_ips history is replayed into the real
    device via record_device_ip(), so source-priority ranking is
    respected (a duplicate's cdp-sourced IP can't downgrade the real
    device's inventory_sync one)
  - any links referencing the duplicate are repointed onto the real
    device (via upsert_link, so a coincidental existing equivalent
    link at the target just updates instead of duplicating)
  - any arp_entries referencing the duplicate are repointed the same way
  - the now-childless duplicate device row is deleted

Usage (run from the project root):
    python3 utilities/merge_duplicate_devices.py

Only merges cases where exactly one real device is found anywhere else
in the database - zero matches (a genuinely new device, nothing to
merge into) or more than one match (ambiguous - which one is really
"it"?) are left alone and reported; use repair_cross_site_links.py for
the zero-match case (it'll file it under Unassigned), and resolve any
ambiguous case by hand.

This performs real deletions of the duplicate rows. The devices/links
being merged were already confirmed as genuine duplicates via SQL
before this was built - back up sad.db first if you want extra safety.
"""

import db


def merge_duplicate(conn, duplicate_id: int, real_id: int, hostname: str) -> None:
    for row in db.get_device_ip_rows(conn, duplicate_id):
        db.record_device_ip(conn, real_id, row["ip"], source=row["source"], raw_source_data=row["raw_source_data"])
    db.delete_device_ip_rows(conn, duplicate_id)

    for link in db.get_links_referencing_device(conn, duplicate_id):
        new_device_a = real_id if link["device_a_id"] == duplicate_id else link["device_a_id"]
        new_device_b = real_id if link["device_b_id"] == duplicate_id else link["device_b_id"]
        db.upsert_link(
            conn,
            link["site_id"],
            new_device_a,
            new_device_b,
            local_intf=link["local_intf"],
            remote_intf=link["remote_intf"],
            source=link["source"],
            raw_source_data=link["raw_source_data"],
        )
        db.delete_link(conn, link["id"])

    db.reassign_arp_entries_device(conn, duplicate_id, real_id)

    db.delete_device(conn, duplicate_id)
    print(f"Merged duplicate '{hostname}' (device id {duplicate_id}) into real device id {real_id}.")


def find_and_merge_all(db_path: str = db.DB_PATH) -> None:
    merged = []
    skipped = []
    vacated_site_ids = set()

    with db.get_conn(db_path) as conn:
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

        already_handled = set()

        for row in rows:
            if row["device_a_site_id"] != row["scanning_site_id"]:
                foreign_id, foreign_hostname, foreign_site_id = row["device_a_id"], row["device_a_hostname"], row["device_a_site_id"]
            elif row["device_b_site_id"] != row["scanning_site_id"]:
                foreign_id, foreign_hostname, foreign_site_id = row["device_b_id"], row["device_b_hostname"], row["device_b_site_id"]
            else:
                continue

            if foreign_id in already_handled:
                continue
            already_handled.add(foreign_id)

            # Search the WHOLE database for another device with this
            # hostname - the real device can be at any site, not just
            # the one that happened to discover this link.
            candidates = [
                m for m in db.find_devices_by_hostname_anywhere(conn, foreign_hostname)
                if m["id"] != foreign_id
            ]

            if len(candidates) == 0:
                skipped.append((foreign_hostname, foreign_id,
                                 "no matching device anywhere else - not a duplicate, it's genuinely new; "
                                 "use repair_cross_site_links.py for this one instead"))
                continue
            if len(candidates) > 1:
                site_list = ", ".join(str(c["owning_site_octet"]) for c in candidates)
                skipped.append((foreign_hostname, foreign_id,
                                 f"ambiguous - matches devices at more than one site ({site_list}); resolve by hand"))
                continue

            real_id = candidates[0]["id"]
            merge_duplicate(conn, foreign_id, real_id, foreign_hostname)
            merged.append((foreign_hostname, foreign_site_id))

        for _, old_site_id in merged:
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM devices WHERE site_id = ?", (old_site_id,)
            ).fetchone()["c"]
            if remaining == 0:
                vacated_site_ids.add(old_site_id)

    print(f"\nMerged: {len(merged)}")
    print(f"Skipped: {len(skipped)}")
    for hostname, dev_id, reason in skipped:
        print(f"  - '{hostname}' (device id {dev_id}): {reason}")

    if vacated_site_ids:
        print(f"\n{len(vacated_site_ids)} site(s) now have zero devices left (not auto-deleted - "
              f"remove manually if you're sure they're just leftover bogus sites):")
        with db.get_conn(db_path) as conn:
            for site_id in vacated_site_ids:
                site = conn.execute("SELECT site_octet FROM sites WHERE id = ?", (site_id,)).fetchone()
                if site:
                    print(f"  - site_octet '{site['site_octet']}' (id {site_id})")


if __name__ == "__main__":
    find_and_merge_all()
