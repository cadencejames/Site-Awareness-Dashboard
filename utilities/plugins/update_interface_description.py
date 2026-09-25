"""
Plugin: Update interface description

Finds every interface whose CURRENT description exactly matches
old_description, and replaces it with new_description. Matching is
exact, not a substring search - a safer default for something that
pushes real config, since a substring match could unexpectedly catch
more interfaces than intended (e.g. old_description="uplink" would
also match "core uplink to DC2" under substring matching). If you
need substring matching instead, that's a deliberate, separate
variant to build - ask rather than assuming this one already does it.
"""

import re

NAME = "Update interface description"
DESCRIPTION = "Finds interfaces with a specific existing description and replaces it with a new one."
PARAMS = [
    {"name": "old_description", "label": "Current description (exact match)"},
    {"name": "new_description", "label": "New description"},
]

_INTERFACE_RE = re.compile(r"^interface\s+(\S+)", re.IGNORECASE)
_DESCRIPTION_RE = re.compile(r"^\s*description\s+(.*\S)\s*$", re.IGNORECASE)


def _scan_interface_descriptions(running_config: str) -> dict:
    """Walks a running-config, tracking which interface stanza we're
    inside, and returns {interface_name: current_description} - only
    for interfaces that actually have a description configured.
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
            desc_match = _DESCRIPTION_RE.match(line)
            if desc_match:
                interfaces[current_interface] = desc_match.group(1)

    return interfaces


def plan(conn, device_row, params):
    """Read-only: pulls this device's running-config and decides what
    (if anything) needs to change here. Never applies anything itself.
    """
    old_description = params.get("old_description", "").strip()
    new_description = params.get("new_description", "").strip()

    if not old_description or not new_description:
        return []
    if old_description == new_description:
        # Nothing would actually change - don't show a no-op "change".
        return []

    running_config = conn.send_command("show running-config")
    interfaces = _scan_interface_descriptions(running_config)

    changes = []
    for interface, current_description in interfaces.items():
        if current_description != old_description:
            continue

        changes.append({
            "description": f'{interface}: description "{current_description}" -> "{new_description}"',
            "commands": [f"interface {interface}", f"description {new_description}"],
        })

    return changes


if __name__ == "__main__":
    # Offline self-test - run this file directly (python3 update_interface_description.py)
    # to exercise plan() against a fake connection and a sample config,
    # with no real device involved. Never triggered by normal plugin
    # loading (list_plugins() imports this file, which does not run
    # this block - only executing it directly does).
    class _FakeConn:
        def send_command(self, cmd):
            return """
interface GigabitEthernet1/0/1
 description Old uplink to core
!
interface GigabitEthernet1/0/2
 description Old uplink to core
!
interface GigabitEthernet1/0/3
 description uplink to core (different text)
!
interface GigabitEthernet1/0/4
 ip address 10.1.4.1 255.255.255.0
!
end
"""

    test_params = {"old_description": "Old uplink to core", "new_description": "New uplink to core-02"}
    changes = plan(_FakeConn(), {"hostname": "test-sw"}, test_params)

    print(f"Found {len(changes)} change(s):")
    for change in changes:
        print(f"  {change['description']}")
        for cmd in change["commands"]:
            print(f"    {cmd}")

    assert len(changes) == 2, f"expected 2 matching interfaces, got {len(changes)}"
    matched = {c["description"].split(":")[0] for c in changes}
    assert matched == {"GigabitEthernet1/0/1", "GigabitEthernet1/0/2"}
    assert changes[0]["commands"] == ["interface GigabitEthernet1/0/1", "description New uplink to core-02"]
    print("\nSelf-test passed.")
