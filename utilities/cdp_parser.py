"""
cdp_parser.py - Parses raw 'show cdp neighbors detail' output into a
list of structured neighbor dicts, ready to feed into db.upsert_device()
/ db.upsert_link().

Handles both IOS-style and NX-OS-style output, which differ slightly:
  - IOS:   "Entry address(es):" / "Management address(es):" / "IP Address:"
  - NX-OS: "Interface Address(es): N" / "Mgmt address(es):" / "IPv4 Address:"

Each neighbor block looks like (IOS example):

    Device ID: site_switch_SW1
    Entry address(es):
      IP Address: 192.168.1.2
    Platform: cisco 9200L, Capabilities Switch IGMP
    Interface: GigabitEthernet0/0/1, Port ID (outgoing port): TenGigabitEthernet1/1/1
    Holdtime : 160 sec
    ...
    Management address(es):
      IP address: 192.168.1.2

separated by a line of dashes ("----...").

IP preference: entry_ip and mgmt_ip are both returned rather than
collapsed into one field. mgmt_ip is generally the right address to
connect to, but it's occasionally configured-but-unreachable (e.g. a
down mgmt0 interface with an IP still assigned) - keeping entry_ip
alongside it means a future connection step can fall back to entry_ip
if mgmt_ip doesn't respond, without needing to re-parse anything.

Filtering: IGNORE_HOSTNAME_PATTERNS below is a simple substring list
for devices you never want treated as real network infrastructure -
phones (Device ID starting "SEP"), leaf/spine switches, etc. Add to
this list as you run into new cases; filter_neighbors() applies it.
Filtering happens as an explicit separate step, not inside
parse_cdp_neighbors(), so the raw parse is never lossy - you always
have the full picture before anything gets excluded.
"""

import re

# Add substrings here as you discover devices to exclude. Matching is
# case-insensitive substring containment against the hostname, so
# "SEP" catches Cisco phone Device IDs like "SEP001122334455", and a
# future addition like "leaf" or "spine" would catch naming-convention
# matches anywhere in the hostname.
IGNORE_HOSTNAME_PATTERNS = ["SEP"]

# Splits the raw output into per-neighbor chunks on a line of 3+ dashes.
_BLOCK_SPLIT_RE = re.compile(r"\n-{3,}\n")

_DEVICE_ID_RE = re.compile(r"Device ID:\s*(\S+)", re.IGNORECASE)
# Some platforms (mostly switch stacks) advertise Device ID as
# "hostname(SERIALNUMBER)" instead of just "hostname" - this splits
# that apart so hostname matching against inventory data (which never
# has the suffix) works correctly, and captures the serial as a real
# field instead of discarding it.
_DEVICE_ID_SERIAL_RE = re.compile(r"^(.*?)\(([^)]+)\)$")
_PLATFORM_RE = re.compile(r"Platform:\s*(.+?),\s*Capabilities", re.IGNORECASE)
_INTERFACE_RE = re.compile(r"Interface:\s*(\S+),\s*Port ID \(outgoing port\):\s*(\S+)", re.IGNORECASE)

# Section-scoped IP extraction: capture everything after the section
# header up to the next blank line (or end of block), then pull the
# first IP out of that chunk. This avoids accidentally grabbing an IP
# from the wrong section when a block has both address sections.
# The [^\n]* after the header consumes NX-OS's trailing count (e.g.
# "Interface Address(es): 1") while still matching IOS's bare header.
_ENTRY_ADDR_SECTION_RE = re.compile(
    r"(?:Entry address\(es\)|Interface address\(es\)):[^\n]*\n(.*?)(?:\n\s*\n|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_MGMT_ADDR_SECTION_RE = re.compile(
    r"(?:Management|Mgmt) address\(es\):[^\n]*\n(.*?)(?:\n\s*\n|\Z)",
    re.DOTALL | re.IGNORECASE,
)
# Matches "IP Address:" (IOS) and "IPv4 Address:" (NX-OS) alike, any case.
_IP_IN_SECTION_RE = re.compile(r"IPv?4?\s+address:\s*([\d.]+)", re.IGNORECASE)


def _extract_ip_from_section(block: str, section_re: re.Pattern) -> str | None:
    section_match = section_re.search(block)
    if not section_match:
        return None
    ip_match = _IP_IN_SECTION_RE.search(section_match.group(1))
    return ip_match.group(1) if ip_match else None


def parse_cdp_neighbors(raw_output: str) -> list[dict]:
    """Parse 'show cdp neighbors detail' output into a list of dicts:
    {hostname, serial_number, entry_ip, mgmt_ip, platform, local_intf,
     remote_intf, raw_block}

    hostname has any trailing "(SERIALNUMBER)" suffix split off (some
    platforms advertise Device ID that way) and captured separately as
    serial_number - None if the Device ID didn't have that suffix.

    Blocks missing a Device ID (e.g. trailing prompt/summary lines) are
    skipped. Blocks missing other fields leave those keys as None rather
    than raising - a partially-parsed neighbor is still worth recording,
    and the raw_block is kept so nothing is lost.

    No filtering happens here - call filter_neighbors() on the result
    if you want IGNORE_HOSTNAME_PATTERNS applied.
    """
    neighbors = []

    for block in _BLOCK_SPLIT_RE.split(raw_output):
        device_id_match = _DEVICE_ID_RE.search(block)
        if not device_id_match:
            continue  # not a neighbor block (leading banner, trailing summary, etc.)

        raw_device_id = device_id_match.group(1)
        serial_match = _DEVICE_ID_SERIAL_RE.match(raw_device_id)
        if serial_match:
            hostname = serial_match.group(1)
            serial_number = serial_match.group(2)
        else:
            hostname = raw_device_id
            serial_number = None

        platform_match = _PLATFORM_RE.search(block)
        platform = platform_match.group(1).strip() if platform_match else None

        interface_match = _INTERFACE_RE.search(block)
        local_intf = interface_match.group(1) if interface_match else None
        remote_intf = interface_match.group(2) if interface_match else None

        entry_ip = _extract_ip_from_section(block, _ENTRY_ADDR_SECTION_RE)
        mgmt_ip = _extract_ip_from_section(block, _MGMT_ADDR_SECTION_RE)
        if mgmt_ip is None:
            mgmt_ip = entry_ip

        neighbors.append({
            "hostname": hostname,
            "serial_number": serial_number,
            "entry_ip": entry_ip,
            "mgmt_ip": mgmt_ip,
            "platform": platform,
            "local_intf": local_intf,
            "remote_intf": remote_intf,
            "raw_block": block.strip(),
        })

    return neighbors


def filter_neighbors(neighbors: list[dict], patterns: list[str] = None) -> list[dict]:
    """Remove neighbors whose hostname contains any of the ignore patterns
    (case-insensitive substring match). Defaults to IGNORE_HOSTNAME_PATTERNS.
    """
    if patterns is None:
        patterns = IGNORE_HOSTNAME_PATTERNS
    patterns_lower = [p.lower() for p in patterns]
    return [
        n for n in neighbors
        if not any(p in n["hostname"].lower() for p in patterns_lower)
    ]


if __name__ == "__main__":
    import sys
    with open(sys.argv[1]) as f:
        all_neighbors = parse_cdp_neighbors(f.read())
    kept = filter_neighbors(all_neighbors)
    print(f"Parsed {len(all_neighbors)}, kept {len(kept)} after filtering")
    for n in kept:
        print(n)
