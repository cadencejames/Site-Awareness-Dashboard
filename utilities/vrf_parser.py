"""
vrf_parser.py - Parses raw 'show vrf' (or 'show ip vrf' on IOS - same
shape, just missing the Protocols column) output into the list of
real VRF names to poll ARP data from. Handles both IOS and NX-OS -
the formats have different headers and columns, but NX-OS's has no
Interfaces column at all, so it doesn't have IOS's multi-interface
continuation-line problem (see below) - it just falls through the
same per-line logic cleanly.

IOS format:

    Name                   Default RD             Interfaces
    CUSTOMER-A             <not set>              Gi0/0

NX-OS format:

    VRF-Name                    VRF-ID State    Reason
    CUSTOMER-A                       1 Up       --

Some platforms also list a second, unrelated table further down for
an internal platform construct (seen so far on one IOS device):

    Platform iVRF Name     iVRF Id                Interfaces
    __PlatformiVRF:_ID00_  0                      LI11/2

That's a different kind of table entirely - different columns
(iVRF Id instead of Default RD), and not something you'd meaningfully
run 'show ip arp vrf <name>' against. Parsing stops at that header
rather than treating its rows as real VRFs, but the caller can still
see it was present via parse_vrf_output()'s platform_section flag if
that's ever useful to know.
"""

import re

_HEADER_RE = re.compile(r"^\s*(?:VRF-)?Name\s+", re.IGNORECASE)
_PLATFORM_SECTION_RE = re.compile(r"^\s*Platform\s+iVRF\s+Name", re.IGNORECASE)


def parse_vrf_names(raw_output: str) -> list[str]:
    """Return the list of real VRF names found in 'show vrf' / 'show ip
    vrf' output, in the order they appear. Stops before any "Platform
    iVRF Name" section if present - see module docstring.

    A VRF with more than one interface gets extra continuation lines
    with no VRF name at all - just padding out to the Interfaces
    column, e.g.:

        MGMT-VRF               65000:200              Po1.99
                                                       Po10.99
                                                       Po11.99

    Those are distinguished from real VRF rows by indentation: a
    continuation line's first non-whitespace character starts at or
    past wherever "Interfaces" began in the header line (found
    dynamically per-table, not a hardcoded column number, since exact
    spacing varies with how wide the Name/Default RD columns are).
    """
    names = []
    in_platform_section = False
    interfaces_col = None

    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if _PLATFORM_SECTION_RE.match(line):
            in_platform_section = True
            interfaces_col = None
            continue
        if in_platform_section:
            continue

        if _HEADER_RE.match(line):
            idx = line.find("Interfaces")
            interfaces_col = idx if idx != -1 else None
            continue

        if "#" in stripped:
            continue  # command echo/prompt line, not a data row

        leading_spaces = len(line) - len(line.lstrip(" "))
        if interfaces_col is not None and leading_spaces >= interfaces_col:
            continue  # continuation line - just another interface for the previous VRF

        parts = stripped.split()
        if parts:
            names.append(parts[0])

    return names


if __name__ == "__main__":
    import sys
    with open(sys.argv[1]) as f:
        raw = f.read()
    vrfs = parse_vrf_names(raw)
    print(f"Found {len(vrfs)} VRF(s): {vrfs}")
