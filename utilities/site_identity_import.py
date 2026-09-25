"""
site_identity_import.py - Bulk-assign site_name and site_code from a CSV,
instead of doing it one at a time in site_manager.py.

Usage (run from the project root):
    python3 utilities/site_identity_import.py path/to/site_identities.csv

Expected CSV columns (exact header, any order):
    octet,code,name

Example:
    octet,code,name
    168,NWK01,Newark
    169,DEN04,Denver

Each row updates the site matching that octet with the given code/name.
A row for an octet that doesn't exist yet in the DB is skipped with a
warning - sites are created by inventory_import.py, not by this script.

site_manager.py's (n)ame/code option is still the right tool for
one-off corrections or newly discovered sites after this bulk load.
"""

import csv
import sys

import db


def import_identities(csv_path: str, db_path: str = db.DB_PATH) -> None:
    updated = 0
    skipped_no_site = 0
    skipped_bad_row = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        expected_fields = set(reader.fieldnames or [])
        required_fields = {"octet", "code", "name"}
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

                octet = row["octet"].strip()
                code = row["code"].strip()
                name = row["name"].strip()

                if not octet:
                    print(f"WARNING: row {row_num} missing octet, skipping: {row}", file=sys.stderr)
                    skipped_bad_row += 1
                    continue

                site = conn.execute(
                    "SELECT id FROM sites WHERE site_octet = ?", (octet,)
                ).fetchone()

                if not site:
                    print(f"WARNING: no site found for octet '{octet}' (row {row_num}) - "
                          f"run inventory_import.py first if this site should exist. Skipping.", file=sys.stderr)
                    skipped_no_site += 1
                    continue

                try:
                    db.set_site_identity(
                        conn,
                        site["id"],
                        site_name=name or None,
                        site_code=code or None,
                    )
                    updated += 1
                except Exception as e:
                    print(f"WARNING: could not update octet '{octet}' (row {row_num}): {e}", file=sys.stderr)
                    skipped_bad_row += 1

    print(f"Updated: {updated}")
    print(f"Skipped (no matching site): {skipped_no_site}")
    print(f"Skipped (bad row / conflict): {skipped_bad_row}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 utilities/site_identity_import.py path/to/site_identities.csv", file=sys.stderr)
        sys.exit(1)
    import_identities(sys.argv[1])
