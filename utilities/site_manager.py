"""
site_manager.py - Interactive CLI for the manual, per-site setup steps
that happen after a Prime import: assigning a human-friendly site_name
and org site_code, and flagging which device is the CDP discovery seed.

Run this after prime_import.py has populated the database (from the
project root):

    python3 utilities/site_manager.py

Both steps are intentionally manual (not inferred), since site naming/
coding is an organizational fact only a person can supply, and picking
the wrong seed device silently would be worse than asking.
"""

import db

DB_PATH = db.DB_PATH


def print_site_row(site) -> str:
    name = site["site_name"] or "(unset)"
    code = site["site_code"] or "(unset)"
    seed = site["seed_device"] or "(none)"
    arp_seed = site["arp_seed_device"] or "(using CDP seed)"
    return f"  [{site['id']}] octet={site['site_octet']:<5} name={name:<20} code={code:<10} seed={seed:<20} arp_seed={arp_seed}"


def list_sites(conn) -> None:
    sites = db.get_all_sites(conn)
    if not sites:
        print("No sites found - run prime_import.py first.")
        return
    print("\n--- Sites ---")
    for site in sites:
        print(print_site_row(site))


def choose_site(conn):
    """Prompt for a site id and return its row, or None if cancelled/invalid."""
    list_sites(conn)
    raw = input("\nEnter site id (blank to cancel): ").strip()
    if not raw:
        return None
    try:
        site_id = int(raw)
    except ValueError:
        print("Not a valid id.")
        return None
    site = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    if not site:
        print("No site with that id.")
        return None
    return site


def list_devices_for_site(conn) -> None:
    """Read-only browse: show every device at a site with its full
    known state (IP, platform, source, serial, seed/ARP-seed status,
    ARP override). Unlike set_seed()/set_arp_seed()/set_arp_override_ip(),
    which only show devices as a means to picking one, this is just for
    looking - e.g. checking what landed in Unassigned, or what's known
    about a site before deciding what (if anything) needs fixing.
    """
    site = choose_site(conn)
    if site is None:
        return
    devices = db.get_devices_for_site(conn, site["id"])
    if not devices:
        print("No devices found for this site.")
        return

    print(f"\n--- Devices at octet {site['site_octet']} ({len(devices)} total) ---")
    for d in devices:
        markers = []
        if d["is_seed"]:
            markers.append("CDP seed")
        if d["is_arp_seed"]:
            markers.append("ARP seed override")
        marker_str = f"  [{', '.join(markers)}]" if markers else ""

        print(f"  [{d['id']}] {d['hostname']}{marker_str}")
        print(f"        mgmt_ip={d['mgmt_ip']}  platform={d['platform']}  source={d['source']}")
        if d["serial_number"]:
            print(f"        serial_number={d['serial_number']}")
        if d["arp_override_ip"]:
            print(f"        arp_override_ip={d['arp_override_ip']}")
        print(f"        last_seen={d['last_seen']}")


def set_identity(conn) -> None:
    site = choose_site(conn)
    if site is None:
        return
    site_name = input(f"New site_name for octet {site['site_octet']} (blank to leave unchanged): ").strip()
    site_code = input(f"New site_code for octet {site['site_octet']} (blank to leave unchanged): ").strip()
    try:
        db.set_site_identity(
            conn,
            site["id"],
            site_name=site_name or None,
            site_code=site_code or None,
        )
        print("Updated.")
    except Exception as e:
        # Most likely a UNIQUE constraint hit (name/code already used elsewhere)
        print(f"Could not update - {e}")


def set_seed(conn) -> None:
    site = choose_site(conn)
    if site is None:
        return
    devices = db.get_devices_for_site(conn, site["id"])
    if not devices:
        print("No devices found for this site.")
        return
    print(f"\n--- Devices at octet {site['site_octet']} ---")
    for d in devices:
        seed_marker = " (current seed)" if d["is_seed"] else ""
        print(f"  [{d['id']}] {d['hostname']:<25} {d['mgmt_ip']:<16} {d['platform']}{seed_marker}")
    raw = input("\nEnter device id to flag as seed (blank to cancel): ").strip()
    if not raw:
        return
    try:
        device_id = int(raw)
    except ValueError:
        print("Not a valid id.")
        return
    matching = [d for d in devices if d["id"] == device_id]
    if not matching:
        print("That device id isn't part of this site.")
        return
    db.set_device_as_seed(conn, site["id"], device_id)
    print(f"'{matching[0]['hostname']}' flagged as seed for this site.")


def set_arp_seed(conn) -> None:
    """Set or clear the ARP-collection seed for a site. Most sites
    never need this - get_arp_seed_device_for_site() already falls
    back to the regular CDP seed automatically. This is only for the
    edge cases where the CDP seed isn't right for ARP (e.g. it isn't a
    router, or doesn't hold the ARP table you actually want).
    """
    site = choose_site(conn)
    if site is None:
        return
    devices = db.get_devices_for_site(conn, site["id"])
    if not devices:
        print("No devices found for this site.")
        return

    current_arp_seed = db.get_arp_seed_device_for_site(conn, site["id"])
    has_explicit_override = any(d["is_arp_seed"] for d in devices)

    print(f"\n--- Devices at octet {site['site_octet']} ---")
    for d in devices:
        markers = []
        if d["is_arp_seed"]:
            markers.append("ARP seed - explicit override")
        elif not has_explicit_override and current_arp_seed and d["id"] == current_arp_seed["id"]:
            markers.append("ARP seed - via CDP seed fallback")
        if d["is_seed"]:
            markers.append("CDP seed")
        marker_str = f" ({', '.join(markers)})" if markers else ""
        print(f"  [{d['id']}] {d['hostname']:<25} {d['mgmt_ip']:<16} {d['platform']}{marker_str}")

    print("\nEnter a device id to set it as the ARP-seed override,")
    print("'c' to clear any override (revert to using the CDP seed),")
    raw = input("or blank to cancel: ").strip()
    if not raw:
        return

    if raw.lower() == "c":
        db.clear_device_as_arp_seed(conn, site["id"])
        print("ARP-seed override cleared - this site will use its CDP seed for ARP collection.")
        return

    try:
        device_id = int(raw)
    except ValueError:
        print("Not a valid id.")
        return
    matching = [d for d in devices if d["id"] == device_id]
    if not matching:
        print("That device id isn't part of this site.")
        return
    db.set_device_as_arp_seed(conn, site["id"], device_id)
    print(f"'{matching[0]['hostname']}' flagged as the ARP-seed override for this site.")


def set_arp_override_ip(conn) -> None:
    """Set or clear a device's arp_override_ip - an address tried
    first during ARP collection specifically (e.g. a shared HSRP/VRRP
    VIP), before falling through to the device's normal ranked IPs.

    This targets any device at the site, not just the current ARP
    seed - for a VIP shared between two real devices, set this
    identically on BOTH of them, so the override still applies
    correctly if the ARP-seed flag ever moves from one to the other.
    """
    site = choose_site(conn)
    if site is None:
        return
    devices = db.get_devices_for_site(conn, site["id"])
    if not devices:
        print("No devices found for this site.")
        return

    print(f"\n--- Devices at octet {site['site_octet']} ---")
    for d in devices:
        override_note = f" (override: {d['arp_override_ip']})" if d["arp_override_ip"] else ""
        print(f"  [{d['id']}] {d['hostname']:<25} {d['mgmt_ip']:<16} {d['platform']}{override_note}")

    raw = input("\nEnter device id to set/clear its ARP override IP (blank to cancel): ").strip()
    if not raw:
        return
    try:
        device_id = int(raw)
    except ValueError:
        print("Not a valid id.")
        return
    matching = [d for d in devices if d["id"] == device_id]
    if not matching:
        print("That device id isn't part of this site.")
        return

    current = matching[0]["arp_override_ip"]
    prompt = f"New override IP for '{matching[0]['hostname']}'"
    prompt += f" (currently {current}, blank to clear): " if current else " (blank to leave unset): "
    new_ip = input(prompt).strip()

    db.set_device_arp_override_ip(conn, device_id, new_ip or None)
    if new_ip:
        print(f"'{matching[0]['hostname']}' will now try {new_ip} first for ARP collection.")
    else:
        print(f"Override cleared for '{matching[0]['hostname']}' - will use its normal ranked IPs.")


def auto_flag_singletons(conn) -> None:
    flagged = db.auto_seed_singleton_sites(conn)
    if not flagged:
        print("Nothing to do - no single-device sites without a seed already set.")
        return
    print(f"Auto-flagged seed for {len(flagged)} site(s):")
    for site, device in flagged:
        print(f"  - site octet {site['site_octet']}: {device['hostname']}")


def main_menu(conn) -> None:
    while True:
        print("\n--- Site Manager ---")
        print("(L)ist sites")
        print("(D)isplay devices at a site")
        print("(N)ame/code a site")
        print("(S)et a site's seed device")
        print("(R)Set/clear a site's ARP-seed override")
        print("(V)Set/clear a device's ARP override IP (e.g. a shared VIP)")
        print("(A)uto-flag seeds for single-device sites")
        print("(Q)uit")
        choice = input("Enter your choice: ").strip().lower()

        if choice == "l":
            list_sites(conn)
        elif choice == "d":
            list_devices_for_site(conn)
        elif choice == "n":
            set_identity(conn)
        elif choice == "s":
            set_seed(conn)
        elif choice == "r":
            set_arp_seed(conn)
        elif choice == "v":
            set_arp_override_ip(conn)
        elif choice == "a":
            auto_flag_singletons(conn)
        elif choice == "q":
            print("Exiting.")
            break
        else:
            print("Invalid choice, please try again.")


if __name__ == "__main__":
    with db.get_conn(DB_PATH) as conn:
        main_menu(conn)
