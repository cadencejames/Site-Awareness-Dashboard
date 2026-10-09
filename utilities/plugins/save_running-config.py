"""
Plugin: Save running-config

Does exactly one thing: saves each device's running-config to its
startup-config. It reads nothing from the config and changes no
settings - every device in scope gets the same single save command.

Syntax is chosen per device: NX-OS uses "copy running-config
startup-config", everything else (IOS/IOS-XE) uses "write memory".
Both are sent with the "do" prefix because the harness applies a
plugin's commands in configuration mode, where plain exec commands
aren't accepted. The harness's own "Save config after committing"
checkbox is NOT needed (and not used) for this plugin - leave it
unticked, otherwise the config is saved twice.

Dry-run only previews the save command per device; nothing is saved
until you run it in commit mode.
"""

NAME = "Save running-config"
DESCRIPTION = "Saves the running-config to startup-config on every device in scope. Changes nothing else."
PARAMS = []


def _is_nxos(conn, device_row) -> bool:
    device_type = getattr(conn, "device_type", "") or ""
    if device_type.startswith("cisco_nxos"):
        return True
    try:
        platform = (device_row["platform"] or "").lower()
    except (KeyError, TypeError, IndexError):
        platform = ""
    return "nexus" in platform or "nx-os" in platform or "nxos" in platform


def plan(conn, device_row, params):
    """Read-only: returns the one save command for this device. Never
    applies anything itself - the harness applies it, and only in
    commit mode."""
    if _is_nxos(conn, device_row):
        command = "do copy running-config startup-config"
    else:
        command = "do write memory"
    return [{"description": "Save running-config to startup-config", "commands": [command]}]


if __name__ == "__main__":
    # Offline self-test: python3 save_running_config.py
    class _Conn:
        def __init__(self, device_type=""):
            self.device_type = device_type

    cases = [
        (_Conn("cisco_ios"), {"platform": "WS-C3850-48P"}, "do write memory"),
        (_Conn("cisco_xe"), {"platform": "C9300-48P"}, "do write memory"),
        (_Conn("cisco_nxos"), {"platform": "N9K-C93180YC-EX"}, "do copy running-config startup-config"),
        (_Conn(""), {"platform": "cisco Nexus9000 C93180YC-EX Chassis"}, "do copy running-config startup-config"),
        (_Conn(""), {"platform": None}, "do write memory"),
    ]
    for conn, device, expected in cases:
        changes = plan(conn, device, {})
        assert len(changes) == 1 and changes[0]["commands"] == [expected], (device, changes)
    print("Self-test passed.")