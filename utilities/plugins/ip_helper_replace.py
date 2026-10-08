"""
Plugin: Replace ip helper-address

Ported from a standalone replace_ip_helper.py script. Finds every
interface with at least one of the old helper IPs configured, and
brings that interface up to the full new-IP set (removing every old
IP found there, adding every new IP not already present) - this
matches the original script's exact behavior: an interface gets BOTH
new IPs added (if missing), not just a 1:1 swap of whichever specific
old IP it had. Preserved deliberately rather than "simplified" during
the port, since it reflects how the original script was actually used.

Works on both IOS-style devices ("ip helper-address X") and Nexus
(NX-OS has no ip helper-address; its equivalent is "ip dhcp relay
address X [use-vrf NAME]"). Which syntax to use is decided PER
INTERFACE from what the interface's own config already contains, not
from the device's platform string, so a mixed or oddly-reported
device still gets the right commands. A use-vrf suffix on a Nexus
relay line is preserved on the replacement lines.
"""

import re

NAME = "Replace ip helper-address"
DESCRIPTION = "Finds interfaces with old ip helper-address values and replaces them with new ones."
PARAMS = [
    {"name": "old_ip_1", "label": "Old IP #1"},
    {"name": "new_ip_1", "label": "New IP #1"},
    {"name": "old_ip_2", "label": "Old IP #2 (optional)", "required": False},
    {"name": "new_ip_2", "label": "New IP #2 (optional)", "required": False},
]

_INTERFACE_RE = re.compile(r"^interface\s+(\S+)", re.IGNORECASE)
_HELPER_RE = re.compile(r"^\s*ip helper-address\s+(\d+\.\d+\.\d+\.\d+)\s*$", re.IGNORECASE)
# NX-OS: "ip dhcp relay address 10.1.1.1" or "... 10.1.1.1 use-vrf MGMT".
# Group 2 is the optional trailing "use-vrf NAME" (kept verbatim so a
# replacement lands in the same VRF as the line it replaces).
_NXOS_RELAY_RE = re.compile(
    r"^\s*ip dhcp relay address\s+(\d+\.\d+\.\d+\.\d+)(\s+use-vrf\s+\S+)?\s*$", re.IGNORECASE
)


def _scan_interface_helpers(running_config: str) -> dict:
    """Walks a running-config, tracking which interface stanza we're
    inside, and returns {interface_name: [helper entries on it]}.
    Each entry is {"ip", "style", "suffix"}: style is "ios"
    (ip helper-address) or "nxos" (ip dhcp relay address), and suffix
    is the NX-OS " use-vrf NAME" tail ("" otherwise).
    """
    interfaces = {}
    current_interface = None

    for line in running_config.splitlines():
        iface_match = _INTERFACE_RE.match(line)
        if iface_match:
            current_interface = iface_match.group(1)
            continue

        # A non-indented line that isn't "interface ..." means we've
        # left the interface stanza (IOS/NX-OS both de-indent to end
        # a block).
        if line and not line.startswith((" ", "\t")):
            current_interface = None
            continue

        if current_interface:
            helper_match = _HELPER_RE.match(line)
            if helper_match:
                interfaces.setdefault(current_interface, []).append(
                    {"ip": helper_match.group(1), "style": "ios", "suffix": ""}
                )
                continue
            relay_match = _NXOS_RELAY_RE.match(line)
            if relay_match:
                interfaces.setdefault(current_interface, []).append(
                    {"ip": relay_match.group(1), "style": "nxos", "suffix": relay_match.group(2) or ""}
                )

    return interfaces


def _command(style: str, ip: str, suffix: str, negate: bool = False) -> str:
    base = f"ip helper-address {ip}" if style == "ios" else f"ip dhcp relay address {ip}{suffix}"
    return f"no {base}" if negate else base


def plan(conn, device_row, params):
    """Read-only: pulls this device's running-config and decides what
    (if anything) needs to change here. Never applies anything itself
    - the harness applies the returned commands, and only in commit
    mode. Returns a list of change-groups:
        [{"description": <human-readable summary>, "commands": [...]}]
    Empty list means nothing on this device needs changing.
    """
    old_to_new = {}
    if params.get("old_ip_1") and params.get("new_ip_1"):
        old_to_new[params["old_ip_1"]] = params["new_ip_1"]
    if params.get("old_ip_2") and params.get("new_ip_2"):
        old_to_new[params["old_ip_2"]] = params["new_ip_2"]

    if not old_to_new:
        return []

    # De-duped, order-preserving - matches the original script's
    # all_new_ips list (built from module constants there, from
    # params here).
    all_new_ips = list(dict.fromkeys(old_to_new.values()))

    running_config = conn.send_command("show running-config")
    interfaces = _scan_interface_helpers(running_config)

    changes = []
    for interface, entries in interfaces.items():
        helper_ips = [e["ip"] for e in entries]
        old_entries = [e for e in entries if e["ip"] in old_to_new]
        if not old_entries:
            continue

        # New lines copy the style (and any NX-OS use-vrf tail) of the
        # first old line found here, so a Nexus interface gets "ip dhcp
        # relay address" lines and an IOS one gets "ip helper-address".
        style = old_entries[0]["style"]
        suffix = old_entries[0]["suffix"]

        # Brings the interface to the FULL new-IP set, not just a 1:1
        # swap of whichever old IP was found - matches the original
        # script's own behavior exactly (see module docstring).
        new_ips_needed = [ip for ip in all_new_ips if ip not in helper_ips]

        commands = [f"interface {interface}"]
        for e in old_entries:
            commands.append(_command(e["style"], e["ip"], e["suffix"], negate=True))
        for new_ip in new_ips_needed:
            commands.append(_command(style, new_ip, suffix))

        removed_label = ", ".join(e["ip"] for e in old_entries)
        added_label = ", ".join(new_ips_needed) if new_ips_needed else "none (already present)"
        changes.append({
            "description": f"{interface}: remove [{removed_label}] / add [{added_label}]",
            "commands": commands,
        })

    return changes


if __name__ == "__main__":
    # Offline self-test - run this file directly (python3 ip_helper_replace.py)
    # to exercise plan() against a fake connection and a sample config,
    # with no real device involved. Never triggered by normal plugin
    # loading (list_plugins() imports this file, which does not run
    # this block - only executing it directly does).
    class _FakeConn:
        def send_command(self, cmd):
            return """
interface GigabitEthernet1/0/1
 ip helper-address 10.10.10.10
 ip helper-address 10.10.20.10
!
interface GigabitEthernet1/0/2
 ip helper-address 10.10.10.10
!
interface GigabitEthernet1/0/3
 ip helper-address 10.10.10.10
 ip helper-address 10.10.10.20
!
interface GigabitEthernet1/0/4
 ip helper-address 8.8.8.8
!
interface Vlan10
  no shutdown
  ip address 10.20.10.1/24
  ip dhcp relay address 10.10.10.10
  ip dhcp relay address 10.10.20.10
!
interface Vlan20
  ip dhcp relay address 10.10.10.10 use-vrf SERVERS
!
interface Vlan30
  ip dhcp relay address 8.8.8.8
!
end
"""

    test_params = {
        "old_ip_1": "10.10.10.10", "new_ip_1": "10.10.10.20",
        "old_ip_2": "10.10.20.10", "new_ip_2": "10.10.20.20",
    }
    changes = plan(_FakeConn(), {"hostname": "test-sw"}, test_params)

    print(f"Found {len(changes)} change(s):")
    for change in changes:
        print(f"  {change['description']}")
        for cmd in change["commands"]:
            print(f"    {cmd}")

    matched = {c["description"].split(":")[0] for c in changes}
    assert matched == {
        "GigabitEthernet1/0/1", "GigabitEthernet1/0/2", "GigabitEthernet1/0/3", "Vlan10", "Vlan20",
    }
    assert "GigabitEthernet1/0/4" not in matched, "an interface with no matching old IP should be untouched"
    assert "Vlan30" not in matched, "a Nexus interface with no matching old IP should be untouched"
    by_iface = {c["description"].split(":")[0]: c["commands"] for c in changes}
    assert by_iface["Vlan10"] == [
        "interface Vlan10",
        "no ip dhcp relay address 10.10.10.10", "no ip dhcp relay address 10.10.20.10",
        "ip dhcp relay address 10.10.10.20", "ip dhcp relay address 10.10.20.20",
    ]
    assert by_iface["Vlan20"] == [
        "interface Vlan20",
        "no ip dhcp relay address 10.10.10.10 use-vrf SERVERS",
        "ip dhcp relay address 10.10.10.20 use-vrf SERVERS", "ip dhcp relay address 10.10.20.20 use-vrf SERVERS",
    ]
    assert by_iface["GigabitEthernet1/0/2"][1].startswith("no ip helper-address"), "IOS interfaces keep IOS syntax"
    print("\nSelf-test passed.")