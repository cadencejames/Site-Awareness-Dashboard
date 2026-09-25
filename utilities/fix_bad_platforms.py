"""
fix_bad_platforms.py - Interactive tool for correcting devices whose
platform string is known garbage from an inventory tool (e.g. Catalyst
Center reporting "Unsupported Cisco Device" for some Nexus models
instead of an actual platform name).

Why this matters beyond just tidiness: orchestrator.py uses platform
substring matching (e.g. "Nexus" in platform) to skip a slow
autodetection step when connecting - a garbage platform value means
every affected device pays that extra cost on every single run,
forever, until it's corrected.

Add new known-bad values to BAD_PLATFORM_VALUES as you find them -
same pattern as cdp_parser.py's IGNORE_HOSTNAME_PATTERNS.

Usage (run from the project root):
    python3 utilities/fix_bad_platforms.py

For each affected device, shows its hostname/site/current platform
and prompts for the correct value. Correcting it tags the device
source='manual' (db.set_device_platform()'s default) - since 'manual'
outranks 'inventory_sync' in the source-priority system, this
correction is protected from being silently overwritten the next time
inventory_import.py runs, unlike a raw SQL UPDATE would be.
"""

import db

# Add more values here as you find them.
BAD_PLATFORM_VALUES = [
    "Unsupported Cisco Device",
]


def main(db_path: str = db.DB_PATH) -> None:
    with db.get_conn(db_path) as conn:
        placeholders = ",".join("?" for _ in BAD_PLATFORM_VALUES)
        rows = conn.execute(
            f"""
            SELECT d.id, d.hostname, d.mgmt_ip, d.platform, s.site_octet, s.site_name
            FROM devices d
            JOIN sites s ON d.site_id = s.id
            WHERE d.platform IN ({placeholders})
            ORDER BY s.site_octet, d.hostname
            """,
            BAD_PLATFORM_VALUES,
        ).fetchall()

        if not rows:
            print("No devices found with a known-bad platform value.")
            return

        print(f"Found {len(rows)} device(s) with a known-bad platform value.\n")
        fixed = 0
        skipped = 0

        for row in rows:
            site_label = row["site_name"] or row["site_octet"]
            print(f"[{row['id']}] {row['hostname']} ({row['mgmt_ip']}) at site {site_label}")
            print(f"    current platform: {row['platform']}")
            new_platform = input("    correct platform (blank to skip): ").strip()

            if not new_platform:
                skipped += 1
                print("    skipped.\n")
                continue

            db.set_device_platform(conn, row["id"], new_platform)
            fixed += 1
            print(f"    updated -> '{new_platform}' (source set to 'manual')\n")

    print(f"Fixed: {fixed}")
    print(f"Skipped: {skipped}")


if __name__ == "__main__":
    main()
