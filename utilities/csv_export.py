"""
csv_export.py - Exports curated CSV inventories from sad.db.

Two exports, both scoped to ALL sites always (not a per-site choice -
these are meant as broad reference files, not something you'd
naturally want filtered at export time):

  - Phone/VTC inventory: every phone/VTC client, correlated with its
    CUCM/RIS enrichment data where available. Choose phones only, VTCs
    only, or both.
  - Device inventory: every device across every site (including the
    reserved "Unassigned" site deliberately - unlike the index page's
    own site-count/site-list, which excludes it, a REFERENCE inventory
    export should be complete, and Unassigned entries arguably need
    more visibility here, not less).

Writes fixed filenames under dashboard/exports/, overwritten on each
run - no dated history kept (matching this project's general "one
current row/file per subject" pattern elsewhere, e.g. mac_table_raw,
phone_enrichment). Immediately regenerates exports.html afterward so
the new/updated file is linked right away - this is the ONLY thing
that ever writes exports.html; the normal dashboard generation cycle
(generate_all()/generate_one()) never touches it, deliberately, so the
Exports page only updates when an export actually runs, not on every
routine "Generate dashboard" click.

Run standalone:
    python3 utilities/csv_export.py --phones-vtc
    python3 utilities/csv_export.py --phones-vtc --phones-only
    python3 utilities/csv_export.py --phones-vtc --vtc-only
    python3 utilities/csv_export.py --devices
    python3 utilities/csv_export.py --phones-vtc --devices
"""

import os
import sys
import csv
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import mac_parser  # noqa: E402
import dashboard_generate  # noqa: E402

EXPORTS_DIR = os.path.join(dashboard_generate.OUTPUT_DIR, "exports")


def _write_csv(path, fieldnames, rows) -> None:
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def export_phone_vtc_inventory(conn, include_phones: bool = True, include_vtcs: bool = True) -> str:
    """Every phone/VTC client across every site, correlated with its
    CUCM/RIS enrichment data via mac_parser.normalize_mac() (not a raw
    SQL join - clients.mac and phone_enrichment.mac aren't guaranteed
    to share the same separator/case format, same reasoning as
    everywhere else this correlation happens). Returns the path
    written.
    """
    wanted_types = set()
    if include_phones:
        wanted_types.add("cisco_phone")
    if include_vtcs:
        wanted_types.add("vtc")

    sites_by_id = {s["id"]: s for s in db.get_all_sites(conn)}
    devices_by_id = {}
    for site_id in sites_by_id:
        for d in db.get_devices_for_site(conn, site_id):
            devices_by_id[d["id"]] = d
    enrichment_by_mac = {row["mac"]: row for row in db.get_all_phone_enrichment(conn)}

    rows = []
    for c in db.get_all_clients(conn):
        if c["device_type"] not in wanted_types:
            continue
        site = sites_by_id.get(c["site_id"])
        switch = devices_by_id.get(c["device_id"])
        enrichment = enrichment_by_mac.get(mac_parser.normalize_mac(c["mac"]))
        rows.append({
            "mac": c["mac"],
            "type": "phone" if c["device_type"] == "cisco_phone" else "VTC",
            "site": (site["site_name"] or site["site_octet"]) if site else "",
            "switch": switch["hostname"] if switch else "",
            "interface": c["interface"] or "",
            "vlan": c["vlan"] or "",
            "model": (enrichment["model"] if enrichment else None) or "",
            "serial_number": (enrichment["serial_number"] if enrichment else None) or "",
            "phone_number": (enrichment["phone_number"] if enrichment else None) or "",
            "ris_status": (enrichment["ris_status"] if enrichment else None) or "",
        })

    path = os.path.join(EXPORTS_DIR, "phones_vtc_inventory.csv")
    fieldnames = ["mac", "type", "site", "switch", "interface", "vlan",
                  "model", "serial_number", "phone_number", "ris_status"]
    _write_csv(path, fieldnames, rows)

    scope = "phones+VTCs" if include_phones and include_vtcs else ("phones only" if include_phones else "VTCs only")
    # Independent connection, not the caller's: the GUI can run this
    # and export_device_inventory() sequentially within one shared
    # transaction (both checkboxes checked is the common case) - if
    # the OTHER export later failed, sharing a connection would risk
    # silently erasing this export's already-successful log record
    # too, even though its CSV genuinely exists on disk.
    db.log_activity("export_phone_vtc_inventory", f"{scope}, {len(rows)} row(s) -> {path}")

    return path


def export_device_inventory(conn) -> str:
    """Every device across every site, including Unassigned. Returns
    the path written.
    """
    rows = []
    for site in db.get_all_sites(conn):
        for d in db.get_devices_for_site(conn, site["id"]):
            is_stale = dashboard_generate._device_is_stale(d, site["last_cdp_discovery"])
            roles = []
            if d["is_seed"]:
                roles.append("CDP seed")
            if d["is_arp_seed"]:
                roles.append("ARP seed")
            rows.append({
                "hostname": d["hostname"],
                "site": site["site_name"] or site["site_octet"],
                "mgmt_ip": d["mgmt_ip"] or "",
                "platform": d["platform"] or "",
                "serial_number": d["serial_number"] or "",
                "source": d["source"] or "",
                "roles": ", ".join(roles),
                "stale": "yes" if is_stale else "no",
            })

    path = os.path.join(EXPORTS_DIR, "device_inventory.csv")
    fieldnames = ["hostname", "site", "mgmt_ip", "platform", "serial_number", "source", "roles", "stale"]
    _write_csv(path, fieldnames, rows)

    # Independent connection - same reasoning as
    # export_phone_vtc_inventory() above.
    db.log_activity("export_device_inventory", f"{len(rows)} row(s) -> {path}")

    return path


def main():
    parser = argparse.ArgumentParser(description="Export curated CSV inventories from sad.db")
    parser.add_argument("--phones-vtc", action="store_true", help="Export the phone/VTC inventory")
    parser.add_argument("--devices", action="store_true", help="Export the device inventory")
    parser.add_argument("--phones-only", action="store_true", help="With --phones-vtc: phones only, not VTCs")
    parser.add_argument("--vtc-only", action="store_true", help="With --phones-vtc: VTCs only, not phones")
    args = parser.parse_args()

    if not args.phones_vtc and not args.devices:
        parser.error("Specify at least one of --phones-vtc or --devices")
    if args.phones_only and args.vtc_only:
        parser.error("--phones-only and --vtc-only are mutually exclusive")

    with db.get_conn() as conn:
        if args.phones_vtc:
            include_phones = not args.vtc_only
            include_vtcs = not args.phones_only
            path = export_phone_vtc_inventory(conn, include_phones, include_vtcs)
            print(f"Wrote {path}")
        if args.devices:
            path = export_device_inventory(conn)
            print(f"Wrote {path}")

    exports_path = dashboard_generate.generate_exports_page()
    print(f"Wrote {exports_path}")


if __name__ == "__main__":
    main()
