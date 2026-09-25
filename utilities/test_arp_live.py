"""
test_arp_live.py - Standalone live test: connect to a real device, run
'show vrf', then pull the global + per-VRF ARP tables, and run
everything through arp_parser.py/vrf_parser.py.

This does NOT touch the database - it's purely for verifying the
connection and both parsers work against real gear before trusting
orchestrator.py's collect_arp() with it.

Usage (run from the project root):
    python3 utilities/test_arp_live.py <device_ip> [--device-type cisco_ios]

Prompts for the master password to unlock credentials.enc (expects
net_user/net_pass keys - add them with credential_manager.py first if
you haven't already).
"""

import sys
import argparse

from netmiko import ConnectHandler

import credential_loader
import arp_parser
import vrf_parser


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

    print("Connected.\n")

    # --- VRF discovery ---
    print("--- Running 'show vrf' ---")
    vrf_raw = conn.send_command("show vrf")
    print(vrf_raw)
    vrf_names = vrf_parser.parse_vrf_names(vrf_raw)
    print(f"--- PARSED: {len(vrf_names)} VRF(s): {vrf_names} ---\n")

    # --- Global ARP table ---
    print("--- Running 'show ip arp' ---")
    global_raw = conn.send_command("show ip arp")
    print(global_raw)
    global_entries = arp_parser.parse_arp_table(global_raw)
    print(f"--- PARSED: {len(global_entries)} entry(ies) ---")
    for e in global_entries:
        print(f"  {e['ip']:<16} {e['mac']}  vlan/intf={e['interface']}  age={e['age_min']}")
    print()

    # --- Per-VRF ARP tables ---
    for vrf_name in vrf_names:
        cmd = f"show ip arp vrf {vrf_name}"
        print(f"--- Running '{cmd}' ---")
        vrf_arp_raw = conn.send_command(cmd)
        print(vrf_arp_raw)
        vrf_entries = arp_parser.parse_arp_table(vrf_arp_raw)
        print(f"--- PARSED: {len(vrf_entries)} entry(ies) for VRF '{vrf_name}' ---")
        for e in vrf_entries:
            print(f"  {e['ip']:<16} {e['mac']}  vlan/intf={e['interface']}  age={e['age_min']}")
        print()

    conn.disconnect()


if __name__ == "__main__":
    main()
