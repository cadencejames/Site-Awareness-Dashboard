"""
repair_seed_flags.py - One-time (or run-whenever-needed) repair tool:
resyncs devices.is_seed from the sites.seed_device text field.

Why this exists: an early version of set_device_as_seed() had a copy-
paste typo (the "set is_seed = 1" line was accidentally another
"set is_seed = 0"), so is_seed never actually got set even though
sites.seed_device (a separate mirror column) was being written
correctly the whole time. This script repairs the drift without
requiring anyone to re-pick every site's seed by hand - seed_device
already has the right hostname, we just need to flip the matching
device's flag.

Usage (run from the project root):
    python3 utilities/repair_seed_flags.py

Safe to re-run - it's idempotent (uses set_device_as_seed(), the same
function normal seed-flagging uses, just driven from the existing
seed_device values instead of manual input).
"""

import db


def repair(db_path: str = db.DB_PATH) -> None:
    fixed = 0
    already_ok = 0
    no_seed_set = []
    seed_hostname_not_found = []

    with db.get_conn(db_path) as conn:
        for site in db.get_all_sites(conn):
            if not site["seed_device"]:
                no_seed_set.append(site["site_octet"])
                continue

            current_seed = db.get_seed_device_for_site(conn, site["id"])
            if current_seed is not None and current_seed["hostname"] == site["seed_device"]:
                already_ok += 1
                continue

            device_id = db.get_device_id_by_hostname(conn, site["id"], site["seed_device"])
            if device_id is None:
                seed_hostname_not_found.append((site["site_octet"], site["seed_device"]))
                continue

            db.set_device_as_seed(conn, site["id"], device_id)
            fixed += 1

    print(f"Fixed (is_seed resynced): {fixed}")
    print(f"Already correct: {already_ok}")
    print(f"Sites with no seed_device set at all: {len(no_seed_set)}")
    if no_seed_set:
        print(f"  -> {no_seed_set}")
    print(f"Sites where seed_device hostname wasn't found in devices: {len(seed_hostname_not_found)}")
    if seed_hostname_not_found:
        print(f"  -> {seed_hostname_not_found}")


if __name__ == "__main__":
    repair()
