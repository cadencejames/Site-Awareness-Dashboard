"""
subnet_override_import.py - Bulk-load subnet exceptions for sites that
share a second octet but are actually split by a non-/24 subnet
boundary.

Usage (run from the project root):
    python3 utilities/subnet_override_import.py path/to/subnet_overrides.csv

Expected CSV columns (exact header):
    cidr,site_key

Example - three sites all living under the 170 second octet, split by
subnet rather than by octet:
    cidr,site_key
    192.170.1.0/25,170a
    192.170.1.128/26,170b
    192.170.1.192/26,170c

site_key can be any string you want that site to be internally known
as (it becomes that site's site_octet value) - it doesn't have to look
like a real octet.

Run this BEFORE inventory_import.py so the overrides are in place when
sites get created/matched. Re-running inventory_import.py after adding
a new override will NOT retroactively move already-imported devices to
the new site - that would need a manual fix, since automatically
reassigning a device's site is exactly the kind of silent judgment
call we've been avoiding elsewhere in this project.
"""

import csv
import sys

import db


def import_overrides(csv_path: str, db_path: str = db.DB_PATH) -> None:
    db.init_db(db_path)

    added = 0
    skipped_bad_row = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        expected_fields = set(reader.fieldnames or [])
        required_fields = {"cidr", "site_key"}
        missing = required_fields - expected_fields
        if missing:
            print(f"ERROR: CSV is missing expected column(s): {sorted(missing)}", file=sys.stderr)
            print(f"Columns found: {reader.fieldnames}", file=sys.stderr)
            sys.exit(1)

        with db.get_conn(db_path) as conn:
            for row_num, row in enumerate(reader, start=2):  # header is line 1
                if None in row or any(v is None for v in row.values()):
                    print(f"WARNING: row {row_num} has a mismatched column count, skipping: {row}", file=sys.stderr)
                    skipped_bad_row += 1
                    continue

                cidr = row["cidr"].strip()
                site_key = row["site_key"].strip()

                if not cidr or not site_key:
                    print(f"WARNING: row {row_num} missing cidr or site_key, skipping: {row}", file=sys.stderr)
                    skipped_bad_row += 1
                    continue

                try:
                    db.upsert_subnet_override(conn, cidr, site_key)
                    added += 1
                except Exception as e:
                    print(f"WARNING: could not add override (row {row_num}): {e}", file=sys.stderr)
                    skipped_bad_row += 1

    print(f"Added/updated: {added}")
    print(f"Skipped (bad row): {skipped_bad_row}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 utilities/subnet_override_import.py path/to/subnet_overrides.csv", file=sys.stderr)
        sys.exit(1)
    import_overrides(sys.argv[1])
