"""
inventory_import.py - Import a device inventory export (from whatever
network management tool is currently in use - Prime Infrastructure,
Catalyst Center, etc.) into SAD's SQLite inventory.

Usage (run from the project root):
    python3 utilities/inventory_import.py path/to/export.csv

Switching source tools: only COLUMN_MAP below should need editing.
Every inventory tool's export uses different column headers, but the
three concepts SAD actually needs are the same regardless of tool:

    hostname  - the device's name
    mgmt_ip   - the IP to reach/manage it at
    platform  - its hardware/software platform string

Map each to whatever your current export actually calls that column,
and everything downstream of COLUMN_MAP stays generic.

What it does:
- Reads the CSV using only the three mapped columns above. Anything
  else in the export (reachability, admin status, collection
  timestamps, software version, etc.) is ignored - not stored, not
  filtered on.
- Determines each device's site via db.resolve_site_key_for_ip(): checks
  the subnet_overrides table first (for sites that share a second octet
  but are split by a non-/24 boundary - manage overrides with
  subnet_override_import.py), then falls back to the plain second-octet
  rule (e.g. 192.168.x.x and 192.169.x.x are two different sites: octet
  "168" and octet "169"). The human-friendly site_name and org site_code
  are NOT set by this script - assign those afterward with
  site_manager.py / site_identity_import.py.
- Imports every row as-is. Any device you don't want in inventory
  (e.g. a decommissioned/unmanaged one) should be removed from the
  source CSV before running this, rather than filtered here.
- Upserts each row into `devices` with source='inventory_sync', and
  records the IP via db.record_device_ip() (also source='inventory_sync')
  rather than writing devices.mgmt_ip directly - inventory_sync is the
  org's system of record, so it always outranks CDP-discovered IPs when
  SAD picks which address to actually connect to. Stashes the original
  row (as JSON) in raw_source_data so you can reprocess later if this
  parsing logic changes.
- Does NOT set is_seed - that stays a manual, per-site decision after
  import, using site_manager.py (or auto_seed_singleton_sites() for the
  no-ambiguity single-device case, run automatically below).

Malformed rows (bad IP, wrong column count) are skipped with a warning
printed to stderr rather than aborting the whole import.
"""

import csv
import sys
import json

import db

# Edit this when switching inventory tools - see module docstring.
COLUMN_MAP = {
    "hostname": "Device Name",
    "mgmt_ip": "IP Address",
    "platform": "Device Type",
}


def import_csv(csv_path: str, db_path: str = db.DB_PATH) -> None:
    db.init_db(db_path)

    imported = 0
    skipped_bad_row = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        expected_fields = set(reader.fieldnames or [])
        required_fields = set(COLUMN_MAP.values())
        missing = required_fields - expected_fields
        if missing:
            print(f"ERROR: CSV is missing expected column(s): {sorted(missing)}", file=sys.stderr)
            print(f"Columns found: {reader.fieldnames}", file=sys.stderr)
            print(f"(Check COLUMN_MAP at the top of this script matches your export's headers.)", file=sys.stderr)
            sys.exit(1)

        with db.get_conn(db_path) as conn:
            for row_num, row in enumerate(reader, start=2):  # header is line 1
                # Defensive check: DictReader puts extra fields under None
                # and marks missing ones as None - this catches a row that
                # didn't split into the right number of columns (e.g. an
                # unquoted comma inside some other field in the export).
                if None in row or any(v is None for v in row.values()):
                    print(f"WARNING: row {row_num} has a mismatched column count, skipping: {row}", file=sys.stderr)
                    skipped_bad_row += 1
                    continue

                hostname = row[COLUMN_MAP["hostname"]].strip()
                mgmt_ip = row[COLUMN_MAP["mgmt_ip"]].strip()
                platform = row[COLUMN_MAP["platform"]].strip()

                if not hostname or not mgmt_ip:
                    print(f"WARNING: row {row_num} missing hostname or IP, skipping: {row}", file=sys.stderr)
                    skipped_bad_row += 1
                    continue

                try:
                    site_octet = db.resolve_site_key_for_ip(conn, mgmt_ip)
                except ValueError:
                    print(f"WARNING: row {row_num} has an unparseable IP '{mgmt_ip}', skipping", file=sys.stderr)
                    skipped_bad_row += 1
                    continue

                raw_row_json = json.dumps(row)

                site_id = db.upsert_site(conn, site_octet)
                device_id = db.upsert_device(
                    conn,
                    site_id=site_id,
                    hostname=hostname,
                    platform=platform,
                    source="inventory_sync",
                    raw_source_data=raw_row_json,
                )
                db.record_device_ip(
                    conn,
                    device_id,
                    mgmt_ip,
                    source="inventory_sync",
                    raw_source_data=raw_row_json,
                )
                imported += 1

    print(f"Imported/updated: {imported}")
    print(f"Skipped (malformed row): {skipped_bad_row}")

    with db.get_conn(db_path) as conn:
        auto_flagged = db.auto_seed_singleton_sites(conn)
    if auto_flagged:
        print(f"Auto-flagged seed for {len(auto_flagged)} single-device site(s):")
        for site, device in auto_flagged:
            print(f"  - site octet {site['site_octet']}: {device['hostname']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 utilities/inventory_import.py path/to/export.csv", file=sys.stderr)
        sys.exit(1)
    import_csv(sys.argv[1])
