"""
test_cdp_live.py - Standalone live test: connect to a real device, pull
'show cdp neighbors detail', and run it through cdp_parser.py.

This does NOT touch the database - it's purely for verifying the
connection and parser work against real gear, independent of
orchestrator.py's own discover_site() walk.

Usage (run from the project root):
    python3 utilities/test_cdp_live.py <device_ip> [--device-type cisco_ios]

Prompts for the master password to unlock credentials.enc (expects
net_user/net_pass keys - add them with credential_manager.py first if
you haven't already).
"""

import sys
import argparse

from netmiko import ConnectHandler

import credential_loader
import cdp_parser


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device_ip", help="Management IP of the device to connect to")
    parser.add_argument("--device-type", default="cisco_ios", help="Netmiko device_type (default: cisco_ios)")
    args = parser.parse_args()

    creds = credential_loader.prompt_and_load()
    if "net_user" not in creds or "net_pass" not in creds:
        print("ERROR: credentials.enc doesn't have 'net_user'/'net_pass' keys. "
              "Add them with credential_manager.py first.", file=sys.stderr)
        sys.exit(1)

    device = {
        "device_type": args.device_type,
        "host": args.device_ip,
        "username": creds["net_user"],
        "password": creds["net_pass"],
    }

    print(f"Connecting to {args.device_ip}...")
    try:
        conn = ConnectHandler(**device)
    except Exception as e:
        print(f"ERROR: connection failed - {e}", file=sys.stderr)
        sys.exit(1)

    print("Connected. Running 'show cdp neighbors detail'...")
    raw_output = conn.send_command("show cdp neighbors detail")
    conn.disconnect()

    print("\n--- RAW OUTPUT ---")
    print(raw_output)

    all_neighbors = cdp_parser.parse_cdp_neighbors(raw_output)
    neighbors = cdp_parser.filter_neighbors(all_neighbors)

    print(f"\n--- PARSED ({len(all_neighbors)} total, {len(neighbors)} kept after filtering) ---")
    for n in neighbors:
        print(f"  hostname={n['hostname']}")
        print(f"    entry_ip={n['entry_ip']}  mgmt_ip={n['mgmt_ip']}  platform={n['platform']}")
        print(f"    local_intf={n['local_intf']}  remote_intf={n['remote_intf']}")


if __name__ == "__main__":
    main()
