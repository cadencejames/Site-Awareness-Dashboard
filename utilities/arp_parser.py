"""
arp_parser.py - Parses raw 'show ip arp' output into a list of
structured entries, ready to feed into db.upsert_arp_entry(). Handles
both IOS and NX-OS output - the two formats look different enough
that detection is done per-line rather than needing to know the
platform up front.

IOS format (every real entry starts with the literal word "Internet"
as the Protocol column - never a valid IP itself, so this is an
unambiguous anchor):

    Internet  192.168.1.1             -    xxxx.xxxx.xxxx   ARPA    GigabitEthernet0/0/1.99
    Internet  192.168.1.50          111    xxxx.xxxx.xxxx   ARPA    GigabitEthernet0/0/1.99

NX-OS format (no such keyword - entries start directly with the IP
address instead, and carry an optional trailing Flags column that's
only present on some rows, so field count varies 4 or 5):

    192.168.1.50    00:13:35  xxxx.xxxx.xxxx  Vlan100
    192.168.1.55    00:08:43  xxxx.xxxx.xxxx  Vlan200              *

Both formats' header/legend/title/command-echo lines are naturally
ignored - they don't start with "Internet" and their first token
never parses as a valid IPv4 address, so no special-casing is needed
to skip them.

age_min keeps whatever the platform reported as-is (IOS: minutes as a
plain number, or "-"; NX-OS: an HH:MM:SS duration, or "-") - nothing
downstream computes with this field, so there's no need to normalize
units across platforms.

A line whose Hardware Addr isn't a real MAC (e.g. IOS's "Incomplete"
for an unresolved entry) is skipped rather than stored -
arp_entries.mac is NOT NULL and a junk value there wouldn't be useful
data anyway.
"""

import re
import ipaddress

_MAC_RE = re.compile(r"^[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}$")


def _is_ipv4(token: str) -> bool:
    try:
        ipaddress.IPv4Address(token)
        return True
    except ValueError:
        return False


def parse_arp_table(raw_output: str) -> list[dict]:
    """Parse 'show ip arp' output (IOS or NX-OS) into a list of dicts:
    {ip, mac, age_min, arp_type, interface, flags, raw_line}

    arp_type is IOS's Type column (e.g. "ARPA") - None for NX-OS,
    which doesn't have an equivalent. flags is NX-OS's optional
    trailing Flags column - None for IOS, and None for NX-OS rows
    that don't have one either.

    Lines that aren't a recognizable ARP entry (header, legend, table
    title, prompt, an unresolved entry, anything with an unexpected
    number of fields) are silently skipped - this stays a pure parser
    with no console output; a caller that wants skip counts should
    compare len(input lines) to len(result) itself.
    """
    entries = []

    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = line.split()

        # IOS: "Internet  <ip>  <age>  <mac>  <type>  <interface>"
        if parts[0].upper() == "INTERNET":
            if len(parts) != 6:
                continue
            _, ip, age, mac, arp_type, interface = parts
            if not _MAC_RE.match(mac):
                continue
            entries.append({
                "ip": ip,
                "mac": mac,
                "age_min": None if age == "-" else age,
                "arp_type": arp_type,
                "interface": interface,
                "flags": None,
                "raw_line": line,
            })
            continue

        # NX-OS: "<ip>  <age>  <mac>  <interface>  [flags]"
        if _is_ipv4(parts[0]):
            if len(parts) not in (4, 5):
                continue
            ip, age, mac, interface = parts[0], parts[1], parts[2], parts[3]
            flags = parts[4] if len(parts) == 5 else None
            if not _MAC_RE.match(mac):
                continue
            entries.append({
                "ip": ip,
                "mac": mac,
                "age_min": None if age == "-" else age,
                "arp_type": None,
                "interface": interface,
                "flags": flags,
                "raw_line": line,
            })
            continue

        # Anything else - header, legend line, "IP ARP Table for
        # context default", "Total number of entries: N", command
        # echo/prompt - isn't a recognizable entry, skip it.

    return entries


if __name__ == "__main__":
    import sys
    with open(sys.argv[1]) as f:
        raw = f.read()
    results = parse_arp_table(raw)
    print(f"Parsed {len(results)} entries")
    for e in results:
        print(e)
