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


def _scan_interface_helpers(running_config: str) -> dict:
    """Walks a running-config, tracking which interface stanza we're
    inside, and returns {interface_name: [all configured helper-
    address IPs on it]}.
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
                interfaces.setdefault(current_interface, []).append(helper_match.group(1))

    return interfaces


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
    for interface, helper_ips in interfaces.items():
        old_ips_found = [ip for ip in helper_ips if ip in old_to_new]
        if not old_ips_found:
            continue

        # Brings the interface to the FULL new-IP set, not just a 1:1
        # swap of whichever old IP was found - matches the original
        # script's own behavior exactly (see module docstring).
        new_ips_needed = [ip for ip in all_new_ips if ip not in helper_ips]

        commands = [f"interface {interface}"]
        for old_ip in old_ips_found:
            commands.append(f"no ip helper-address {old_ip}")
        for new_ip in new_ips_needed:
            commands.append(f"ip helper-address {new_ip}")

        removed_label = ", ".join(old_ips_found)
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
    assert matched == {"GigabitEthernet1/0/1", "GigabitEthernet1/0/2", "GigabitEthernet1/0/3"}
    assert "GigabitEthernet1/0/4" not in matched, "an interface with no matching old IP should be untouched"
    print("\nSelf-test passed.")
