"""
mac_parser.py - Parses "show mac address-table" output (IOS and NX-OS,
format auto-detected per line - no platform flag needed, matching how
arp_parser.py/vrf_parser.py already handle this) into a list of
{mac, vlan, interface, raw_line} dicts, plus normalize_mac() for
comparing MAC addresses reported in different separator styles.

This is one of three raw data sources that feed client tracking (the
MAC address table itself, the existing `links` table, and CDP's SEP-
prefixed phone entries) - this module only extracts the raw MAC-table
rows. Deciding which of those rows represent a real end device versus
an inter-switch uplink (by checking against `links`) happens in a
separate correlation pass elsewhere, once a site's full topology is
known - not here.
"""

import re

# Any run of 2-5 hex-digit groups (1-4 hex digits each) separated by
# '.', ':', or '-', followed by one final group. Deliberately broad
# rather than hand-enumerating every specific grouping style (Cisco's
# native 3-group dot notation aabb.ccdd.eeff, 6-group colon notation
# aa:bb:cc:dd:ee:ff, 6-group dot notation aa.bb.cc.dd.ee.ff, 3-group
# colon notation aabb:ccdd:eeff, dash-separated, etc.) - real-world
# output varies across platforms and tools, and this catches all of
# them the same way. _find_mac_token() below then verifies the total
# hex-digit count is exactly 12 (a real MAC's length) before accepting
# a candidate, which is what actually rejects anything that merely
# looks vaguely MAC-shaped.
_MAC_CANDIDATE_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}[.:-]){2,5}[0-9a-fA-F]{1,4}\b")

# VLAN is the first token on the line in both IOS and NX-OS output;
# NX-OS additionally prefixes some rows with a single marker character
# (* for a primary entry, + for one learned via a vPC peer-link).
_VLAN_RE = re.compile(r"^[*+]?\s*(\d+)\s")

# Ports that aren't a real physical client location - the switch's own
# control-plane/management entries, not an end device.
_NON_CLIENT_PORTS = {"cpu", "router", "switch"}

# A "port" like Vl1/Vl100/Vl2001 (Cisco's abbreviation for a VLAN SVI)
# isn't a real physical port at all - it means the switch learned this
# MAC on its OWN logical Layer-3 interface for that VLAN, which is the
# switch reporting something about itself (or about a genuine
# neighbor's routed uplink, already caught separately by the
# known-link check in orchestrator.py), not a client plugged into a
# physical port. The VLAN number varies per entry, so this needs a
# pattern, not another fixed name in _NON_CLIENT_PORTS above.
_SVI_PORT_RE = re.compile(r"^vl\d+$", re.IGNORECASE)

# A "sup-eth" port (a supervisor module's own management/control-plane
# interface, e.g. on modular Nexus chassis platforms) is, like CPU,
# the switch's own control-plane, not a real physical client port. The
# exact name varies - sup-eth1, sup-eth2, and can carry a trailing
# marker like "sup-eth1(R)" for the active/routed supervisor - a fixed
# exact-match list can't keep up with that, so this is a prefix
# pattern instead, matching any sup-prefixed port regardless of number
# or trailing decoration.
_SUP_PORT_RE = re.compile(r"^sup", re.IGNORECASE)


def normalize_mac(raw_mac: str) -> str:
    """Strips every non-hex-digit character and lowercases what's left,
    so aabb.ccdd.eeff / aa:bb:cc:dd:ee:ff / aa.bb.cc.dd.ee.ff /
    aabb:ccdd:eeff / AA-BB-CC-DD-EE-FF all compare equal. Different
    platforms (and even different commands on the same platform)
    report MAC addresses in different separator styles and cases - a
    plain string comparison would otherwise silently treat the same
    physical MAC as two different values, with no error to notice.
    Used both when deduplicating a client seen from multiple switches
    and when correlating against arp_entries.mac for display.
    """
    return re.sub(r"[^0-9a-fA-F]", "", raw_mac or "").lower()


def _find_mac_token(line: str):
    """Finds the MAC-shaped token on a line, if any. See
    _MAC_CANDIDATE_RE's comment for why this is a broad candidate
    search plus a length check, rather than one narrow pattern."""
    for candidate in _MAC_CANDIDATE_RE.findall(line):
        if len(normalize_mac(candidate)) == 12:
            return candidate
    return None


def parse_mac_table(raw_output: str) -> list:
    """Parses 'show mac address-table' output into a list of
    {mac, vlan, interface, raw_line} dicts.

    Both "dynamic" and "static" entries are kept - static was
    originally assumed to mean administratively-configured
    infrastructure/control-plane noise, but on some environments
    (e.g. port-security "sticky" MAC learning, common on voice-capable
    access ports) it's the normal, expected type for a genuine client
    device like a phone. The real noise filtering happens via the
    port checks below instead: entries whose port is something like
    "CPU" (the switch's own control-plane, not a real physical port)
    are skipped, as are entries with a comma-separated port list (seen
    on some flooding-type entries) - there's no single real client
    location to report for those, so skipping is more honest than
    guessing. Header/legend/separator lines are excluded naturally,
    since they never have a genuine MAC-shaped token on them.
    """
    results = []
    for line in raw_output.splitlines():
        mac = _find_mac_token(line)
        if not mac:
            continue

        vlan_match = _VLAN_RE.match(line)
        vlan = vlan_match.group(1) if vlan_match else None

        tokens = line.split()
        if not tokens:
            continue
        port = tokens[-1]
        if port.lower() in _NON_CLIENT_PORTS or "," in port or _SVI_PORT_RE.match(port) or _SUP_PORT_RE.match(port):
            continue

        results.append({
            "mac": mac,
            "vlan": vlan,
            "interface": port,
            "raw_line": line.strip(),
        })
    return results
