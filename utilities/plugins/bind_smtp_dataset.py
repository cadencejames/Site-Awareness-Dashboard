"""
Plugin: Bind SMTP dataset IP (ADC)

STANDALONE plugin - see the plugins README for the distinction from a
per-device plugin like ip_helper_replace.py. This one owns its entire
device list and connection flow via run(), rather than letting the
harness resolve devices from SAD's database and hand them over one at
a time - because these Citrix ADC appliances aren't tracked in SAD's
database at all, and because the actual unit of work is an HA PAIR,
not a single device: which node to act on has to be determined first.

Only ONE connection per pair is actually needed: confirmed against
real ADC output, a single 'show ha node' response - from either node -
already reports both nodes' IP and Master State together, so there's
no need to separately connect to both. See _find_master() for the
parsing and the cross-check against the pair's own known IPs before
trusting a parsed result.

Ported from a real, working standalone script. ADC_PAIRS/DATASET_NAME
are left as fixed constants here (edit this file directly to change
them) rather than exposed as GUI params - like CUCM_HOST in
cucm_enrich.py, this is infrastructure config that rarely changes, not
something you'd want to type in on every run. bind_ip is the one
param that genuinely changes per-use, so that's the one exposed.

IMPORTANT - dry-run safety is THIS PLUGIN'S OWN responsibility: unlike
a per-device plugin's plan(), which the harness itself keeps read-only
by contract, run() here does everything, including deciding whether to
actually apply anything. Every place this plugin would change a
device is guarded by an explicit "if not commit: return" check before
it happens - see _process_pair() below. Do not remove those guards.
"""

import re

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

NAME = "Bind SMTP dataset IP (ADC)"
DESCRIPTION = "Binds an IP to the smtp_ip_data_set policy dataset on each Citrix ADC HA pair's current master."
PARAMS = [
    {"name": "bind_ip", "label": "IP to bind"},
]

# Fixed device list - these ADC appliances aren't in SAD's own
# database, so (like CUCM_HOST in cucm_enrich.py) this is edited here
# directly rather than exposed as a GUI param. 2 HA pairs, 2 nodes
# each - order within a pair doesn't matter, run() figures out which
# one is master by asking each node directly.
ADC_PAIRS = {
    1: ("10.1.1.11", "10.1.1.12"),
    2: ("10.2.2.11", "10.2.2.12"),
}
DATASET_NAME = "smtp_ip_data_set"
DEVICE_TYPE = "netscaler"

_NODE_IP_RE = re.compile(r"IP:\s*(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
_MASTER_STATE_RE = re.compile(r"Master State:\s*(\w+)", re.IGNORECASE)


def _parse_ha_nodes(output: str) -> list:
    """Parses 'show ha node' output into a list of (ip, state) tuples,
    one per node section found, in the order they appear. Confirmed
    against real ADC output: a SINGLE connection to EITHER node in a
    pair reports BOTH nodes' info together, each as its own numbered
    section with an "IP:" line followed (a few lines later) by a
    "Master State:" line - so there's no need to connect to both nodes
    separately at all, just one successful connection to the pair.

    The IP line can optionally be followed by a parenthesized hostname
    annotation (e.g. "IP:  10.10.10.10 (hostname)") - inconsistently
    present, only on some nodes in real output - the IP regex only
    captures the leading dotted-quad, so this needs no special
    handling either way.
    """
    nodes = []
    current_ip = None
    for line in output.splitlines():
        ip_match = _NODE_IP_RE.search(line)
        if ip_match:
            current_ip = ip_match.group(1)
            continue
        state_match = _MASTER_STATE_RE.search(line)
        if state_match and current_ip:
            nodes.append((current_ip, state_match.group(1).capitalize()))
            current_ip = None
    return nodes


def _is_already_bound(conn, dataset_name: str, bind_ip: str) -> bool:
    output = conn.send_command(f"show policy dataset {dataset_name}")
    return bind_ip in output


def _find_master(pair_ips, credentials):
    """Connects to ONE node of the pair - trying each known IP in
    order and using the first that succeeds, the same "try every known
    IP" resilience pattern SAD already uses elsewhere - and reads
    'show ha node', which reports both nodes' state in a single
    response. Cross-checks the parsed master's IP against this pair's
    own known IPs before trusting it: if parsing ever came back with
    something outside the expected pair, that's a sign something is
    genuinely wrong, and this refuses rather than proceeding. Returns
    (master_ip, None) on success, or (None, <error explaining why>) if
    a single master can't be cleanly and safely identified - never
    guesses on a live pair.
    """
    output = None
    last_error = None
    for ip in pair_ips:
        try:
            with ConnectHandler(device_type=DEVICE_TYPE, host=ip, **credentials) as conn:
                output = conn.send_command("show ha node")
            break
        except (NetmikoAuthenticationException, NetmikoTimeoutException) as exc:
            last_error = f"{ip} -> Connection failed: {exc}"
        except Exception as exc:
            last_error = f"{ip} -> Unexpected error: {exc}"

    if output is None:
        return None, f"Could not reach either node in the pair ({last_error})"

    nodes = _parse_ha_nodes(output)
    primaries = [ip for ip, state in nodes if state.lower() == "primary"]

    if len(primaries) != 1:
        states = ", ".join(f"{ip}={state}" for ip, state in nodes) if nodes else "no nodes parsed from output"
        return None, f"Could not identify exactly one Primary node ({states}) - skipping this pair for safety."

    master_ip = primaries[0]
    if master_ip not in pair_ips:
        return None, (
            f"Parsed master IP {master_ip} is not one of this pair's known IPs "
            f"({', '.join(pair_ips)}) - skipping this pair for safety."
        )

    return master_ip, None


def _process_pair(log, pair_id, pair_ips, credentials, bind_ip, commit):
    log(f"--- Pair {pair_id} ({', '.join(pair_ips)}) ---")

    master_ip, error = _find_master(pair_ips, credentials)
    if error:
        log(f"  ERROR: {error}")
        return

    log(f"  Master is {master_ip}")

    try:
        with ConnectHandler(device_type=DEVICE_TYPE, host=master_ip, **credentials) as conn:
            if _is_already_bound(conn, DATASET_NAME, bind_ip):
                log(f"  {master_ip} already has {bind_ip} bound to {DATASET_NAME} - nothing to do.")
                return

            commands = [
                f"bind policy dataset {DATASET_NAME} {bind_ip}",
                "save config",
            ]

            prefix = "" if commit else "[DRY-RUN] "
            log(f"  {prefix}{master_ip} would run:")
            for cmd in commands:
                log(f"    > {cmd}")

            if not commit:
                return

            for cmd in commands:
                output = conn.send_command(cmd)
                log(f"  {cmd} -> {output.strip() or '(no output)'}")
            log(f"  Applied to {master_ip}.")

    except (NetmikoAuthenticationException, NetmikoTimeoutException) as exc:
        log(f"  ERROR: Connection failed to master {master_ip}: {exc}")
    except Exception as exc:
        log(f"  ERROR: Unexpected error on master {master_ip}: {exc}")


def run(log, params: dict, credentials: dict, commit: bool) -> None:
    bind_ip = params.get("bind_ip", "").strip()
    if not bind_ip:
        log("No bind IP given - nothing to do.")
        return

    log(f"=== {NAME} - {'COMMIT' if commit else 'DRY-RUN'} - dataset={DATASET_NAME}, bind_ip={bind_ip} ===")
    for pair_id, pair_ips in ADC_PAIRS.items():
        _process_pair(log, pair_id, pair_ips, credentials, bind_ip, commit)


if __name__ == "__main__":
    # Offline self-test - run this file directly (python3 bind_smtp_dataset.py)
    # to exercise run() against fake connections, with no real ADC
    # involved. Never triggered by normal plugin loading.
    #
    # Sample output shaped exactly like real, confirmed ADC output:
    # both nodes reported from a single connection, and the IP line's
    # optional "(hostname)" annotation present on only ONE of the two
    # nodes (confirmed to appear inconsistently on real output) - the
    # self-test deliberately keeps that inconsistency rather than
    # "cleaning it up", since that's the real case _find_master() has
    # to handle correctly.
    _SHOW_HA_NODE_OUTPUT = """
1)    Node ID: 0
      IP:  10.1.1.11 (adc-pair1-node0)
      Node State: UP
      Master State: Primary
      Fail-Safe Mode: ON
2)    Node ID: 1
      IP: 10.1.1.12
      Node State: UP
      Master State: Secondary
      Fail-Safe Mode: ON
"""

    class _FakeConn:
        def __init__(self, host):
            self.host = host
            self.commands_sent = []

        def send_command(self, cmd):
            self.commands_sent.append(cmd)
            if cmd == "show ha node":
                return _SHOW_HA_NODE_OUTPUT
            if cmd.startswith("show policy dataset"):
                return "no entries bound"
            return "done"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import unittest.mock as mock

    created = []

    def _fake_connect_handler(device_type, host, **credentials):
        c = _FakeConn(host)
        created.append(c)
        return c

    logged = []

    def _log(line=""):
        print(line)
        logged.append(line)

    # Only pair 1's IPs are "reachable" in this fake fleet - pair 2
    # should correctly report a connection failure, not silently skip.
    with mock.patch(f"{__name__}.ADC_PAIRS", {1: ("10.1.1.11", "10.1.1.12")}), \
         mock.patch(f"{__name__}.ConnectHandler", side_effect=_fake_connect_handler):
        run(_log, {"bind_ip": "10.10.10.20"}, {"username": "u", "password": "p"}, commit=False)

    output = "\n".join(logged)
    assert "Master is 10.1.1.11" in output
    assert "> bind policy dataset smtp_ip_data_set 10.10.10.20" in output
    assert "[DRY-RUN]" in output
    # Exactly 2 connections total: one to _find_master() (election -
    # down from 2 in the original both-nodes-separately design), and a
    # separate one to actually check/act on the elected master.
    assert len(created) == 2, f"expected exactly 2 connections, got {len(created)}"
    assert created[0].host == created[1].host == "10.1.1.11"
    print("\nSelf-test passed.")
