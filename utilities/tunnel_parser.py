"""
tunnel_parser.py - Parses raw 'show interfaces tunnel <N>' output (one
or more tunnels concatenated together, e.g. from looping the command
over every "Tu" interface found in 'show interface description') into
a list of structured entries.

This is the OPERATIONAL/EXEC form of the command, not the running-config
form ('show running-config interface tunnel <N>') - that distinction
matters because the running-config version can show the tunnel source
as a configured interface name (e.g. "tunnel source GigabitEthernet0/1")
instead of a resolved IP, while the operational form resolves it to the
actual literal address this device is using. This parser is built
against the operational form only.

Real (redacted) sample this was built from:

    <hostname># show int Tuxxxxxx
    Tunnelxxxxxx is up, line protocol is up
      Hardware is Tunnel
      Description: Tunnel to site <site_code>
      Internet address is: <wan IP/subnet mask>
      MTU xxxx bytes, BW 100000 Kbit/sec, DLY 10000 usec,
        reliability 255/255, txload 1/255, rxload 1/255
      Encapsulation TUNNEL, loopback not set
      Tunnel linestate evaluation up
      Tunnel source <ip address>, destination <ip address>
    <... Other stats here are unnecessary...>

Each tunnel's block is anchored by its first line - "<Interface> is
<status>, line protocol is <status>" - the same style anchor as a
normal 'show interfaces' block. Everything from one anchor line up to
(but not including) the next anchor line (or end of output) belongs to
that tunnel.

Within a block, three things are pulled out:
  - Description: - optional; not every tunnel necessarily has one
    configured, so its absence is not an error.
  - Internet address is: <ip>/<mask> - the tunnel's own inside/logical
    IP. Also optional - an unconfigured tunnel interface can report
    "Internet address is: not set" (or similar) instead of an address,
    which is kept as None rather than a junk string.
  - Tunnel source <ip>, destination <ip> - the pair used elsewhere to
    match this tunnel up against its far-end device's own tunnel row.
    This is the field that matters most for matching, so it's parsed
    defensively: the confirmed real format is a bare IP on each side,
    but Cisco has another documented form where the source (and in
    principle the destination) can carry the resolved interface name
    in parentheses right after the IP, e.g.:

        Tunnel source 10.1.1.1 (GigabitEthernet0/1), destination 10.2.2.2

    This hasn't been seen in this environment's real output yet, but
    hasn't been ruled out either, so both source and destination are
    matched with an optional "(InterfaceName)" suffix. When present,
    it's kept in source_interface/destination_interface; when absent,
    those are just None and only the IPs matter.

A tunnel block with no recognizable "Tunnel source ..." line is still
returned (with source_ip/destination_ip as None) rather than dropped -
seeing a tunnel that failed to parse its source/destination is more
useful for troubleshooting than silently losing it, unlike arp_parser's
policy of dropping unresolvable rows outright.
"""

import re

_ANCHOR_RE = re.compile(
    r"^(?P<intf>\S+) is (?P<admin_status>up|down|administratively down)"
    r"(?:,\s*line protocol is (?P<line_protocol>up|down))?",
    re.IGNORECASE,
)

_DESCRIPTION_RE = re.compile(r"^Description:\s*(.+)$", re.IGNORECASE)

_INTERNET_ADDR_RE = re.compile(r"^Internet address is:?\s*(.+)$", re.IGNORECASE)

# Defensive: source/destination each optionally carry "(InterfaceName)"
# right after the IP - not confirmed to occur in this environment, but
# handled in case it does. See module docstring.
_TUNNEL_SRC_DST_RE = re.compile(
    r"^Tunnel source\s+(?P<src_ip>\S+?)(?:\s*\((?P<src_intf>[^)]+)\))?,"
    r"\s*destination\s+(?P<dst_ip>\S+?)(?:\s*\((?P<dst_intf>[^)]+)\))?\s*$",
    re.IGNORECASE,
)


def _flush(block_lines: list[str], anchor_match: "re.Match") -> dict:
    entry = {
        "interface": anchor_match.group("intf"),
        "admin_status": anchor_match.group("admin_status").lower(),
        "line_protocol": (anchor_match.group("line_protocol") or "").lower() or None,
        "description": None,
        "internet_address": None,
        "source_ip": None,
        "source_interface": None,
        "destination_ip": None,
        "destination_interface": None,
        "raw_line": None,
    }

    for line in block_lines:
        line = line.strip()
        if not line:
            continue

        if entry["description"] is None:
            m = _DESCRIPTION_RE.match(line)
            if m:
                entry["description"] = m.group(1).strip()
                continue

        if entry["internet_address"] is None:
            m = _INTERNET_ADDR_RE.match(line)
            if m:
                addr = m.group(1).strip()
                # "not set" / "no address" style responses - keep as
                # None rather than storing the junk string.
                if addr.lower() not in ("not set", "no address", "unassigned"):
                    entry["internet_address"] = addr
                continue

        if entry["source_ip"] is None:
            m = _TUNNEL_SRC_DST_RE.match(line)
            if m:
                entry["source_ip"] = m.group("src_ip")
                entry["source_interface"] = m.group("src_intf")
                entry["destination_ip"] = m.group("dst_ip")
                entry["destination_interface"] = m.group("dst_intf")
                entry["raw_line"] = line
                continue

    return entry


def parse_tunnel_interfaces(raw_output: str) -> list[dict]:
    """Parse one or more 'show interfaces tunnel <N>' blocks into a
    list of dicts:
    {interface, admin_status, line_protocol, description,
     internet_address, source_ip, source_interface, destination_ip,
     destination_interface, raw_line}

    admin_status/line_protocol are lowercased ("up", "down",
    "administratively down"); line_protocol is None if the anchor line
    didn't include a ", line protocol is ..." clause.

    source_interface/destination_interface are only ever populated if
    the device's output includes the parenthetical interface-name form
    described in the module docstring - otherwise they're None and
    only *_ip matters for matching tunnels across devices.

    A block whose source/destination line didn't match anything
    recognizable is still returned (source_ip/destination_ip left as
    None) rather than dropped, so a caller can surface "found a tunnel
    interface but couldn't parse its source/destination" instead of
    silently losing it.

    Command-echo/prompt lines, "Hardware is Tunnel", MTU/bandwidth/
    reliability lines, "Encapsulation TUNNEL...", "Tunnel linestate
    evaluation ..." and anything else not explicitly handled above are
    ignored.
    """
    entries = []
    current_anchor = None
    current_block: list[str] = []

    def flush_if_open():
        if current_anchor is not None:
            entries.append(_flush(current_block, current_anchor))

    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        anchor_match = _ANCHOR_RE.match(line)
        if anchor_match:
            flush_if_open()
            current_anchor = anchor_match
            current_block = []
            continue

        if current_anchor is not None:
            current_block.append(line)
        # Lines before any anchor is seen (e.g. a leading command-echo
        # prompt like "<hostname># show int Tuxxxxxx") are ignored.

    flush_if_open()

    return entries


def parse_tunnel_interface_names(raw_output: str) -> list[str]:
    """Return every interface name from 'show interface description'
    (or 'show interfaces description') output whose name starts with
    "Tu" - Cisco's naming prefix for Tunnel interfaces, not shared
    with any other interface type (e.g. TenGigE uses "Te", not "Tu").

    Used as a lightweight first pass to enumerate which tunnel
    interfaces exist on a device before running
    'show interfaces <name>' against each one individually - this
    command's own output (just an up/down summary and a description)
    isn't enough on its own to get source/destination, which is why
    it's a separate pass rather than trying to extract everything from
    this output alone.

    The name is returned exactly as the device reports it - either the
    abbreviated "Tu100" or full "Tunnel100" form, depending on
    platform/terminal width - either form works equally well as the
    interface argument to 'show interfaces <name>'.

    Command-echo/prompt lines and the header row are skipped without
    special-casing them - neither one's first token starts with "tu",
    so they're naturally excluded by the same check as everything else.
    """
    names = []
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped or "#" in stripped:
            continue
        first = stripped.split()[0]
        if first.lower().startswith("tu"):
            names.append(first)
    return names


if __name__ == "__main__":
    import sys
    with open(sys.argv[1]) as f:
        raw = f.read()
    results = parse_tunnel_interfaces(raw)
    print(f"Parsed {len(results)} tunnel interface(s)")
    for e in results:
        print(e)