"""
config_parser.py - cleans raw `show running-config` output for storage.

clean_config() strips the header lines that change on every pull (so an
unchanged config hashes the same), pulls the device's own "last
configuration change" metadata out of them, and sanity-checks that the
output really is a complete running-config (a denied command or a cut-off
pull must never be stored as a snapshot). Works for IOS/IOS-XE and NX-OS.
"""
import re

# Header/footer lines that differ pull to pull without the config changing.
_VOLATILE = [
    re.compile(r"^Building configuration\.\.\.\s*$"),
    re.compile(r"^Current configuration\s*:\s*\d+ bytes\s*$"),
    re.compile(r"^Using \d+ out of \d+ bytes.*$"),
    re.compile(r"^!\s*No configuration change since last restart\s*$"),
    re.compile(r"^ntp clock-period \d+\s*$"),
    re.compile(r"^!Command: show running-config.*$"),
    re.compile(r"^!Time: .*$"),
]
# "! Last configuration change at <time> by <user>" (IOS/IOS-XE)
_LAST_CHANGE = re.compile(r"^!\s*Last configuration change at (?P<when>.+?)(?:\s+by\s+(?P<who>\S+))?\s*$")
_NVRAM = re.compile(r"^!\s*NVRAM config last updated at (?P<when>.+?)(?:\s+by\s+(?P<who>\S+))?\s*$")
# "!Running configuration last done at: <time>" (NX-OS; no user is given)
_NXOS_DONE = re.compile(r"^!Running configuration last done at:\s*(?P<when>.+?)\s*$")
_ERROR_MARKERS = ("% invalid", "% incomplete", "% ambiguous", "authorization failed", "% authorization", "command rejected")


def clean_config(raw: str) -> dict:
    """Returns {"text", "changed_at", "changed_by", "nvram_updated", "problem"}.
    `problem` is None for a usable config, else a short reason; callers
    must not store a config whose problem is set."""
    result = {"text": "", "changed_at": None, "changed_by": None, "nvram_updated": None, "problem": None}
    lines = [ln.rstrip() for ln in (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    if not any(lines):
        result["problem"] = "empty output from 'show running-config'"
        return result

    nxos = any(ln.startswith("!Command:") or ln.startswith("!Running configuration last done") for ln in lines)
    kept = []
    for ln in lines:
        m = _LAST_CHANGE.match(ln)
        if m:
            result["changed_at"], result["changed_by"] = m.group("when"), m.group("who")
            continue
        m = _NVRAM.match(ln)
        if m:
            result["nvram_updated"] = m.group("when")
            continue
        m = _NXOS_DONE.match(ln)
        if m:
            result["changed_at"] = m.group("when")
            continue
        if any(rx.match(ln) for rx in _VOLATILE):
            continue
        kept.append(ln)

    while kept and not kept[0]:
        kept.pop(0)
    while kept and not kept[-1]:
        kept.pop()

    head = " ".join(kept[:3]).lower()
    if any(marker in head for marker in _ERROR_MARKERS):
        result["problem"] = "device rejected the command: " + (kept[0] if kept else "")[:120]
    elif not any(ln.startswith("hostname ") for ln in kept):
        result["problem"] = "output does not look like a running-config (no hostname line)"
    elif not nxos and kept[-1] != "end":
        result["problem"] = "config looks cut off (IOS output did not finish with 'end')"
    else:
        result["text"] = "\n".join(kept) + "\n"
    return result


if __name__ == "__main__":
    ios = (
        "Building configuration...\n\nCurrent configuration : 1234 bytes\n!\n"
        "! Last configuration change at 14:22:01 UTC Mon Oct 19 2026 by jsmith\n"
        "! NVRAM config last updated at 14:22:05 UTC Mon Oct 19 2026 by jsmith\n!\n"
        "version 15.2\nhostname sw1\nntp clock-period 17179869\nend\n"
    )
    r = clean_config(ios)
    assert r["problem"] is None, r
    assert r["changed_at"] == "14:22:01 UTC Mon Oct 19 2026" and r["changed_by"] == "jsmith", r
    assert r["nvram_updated"] == "14:22:05 UTC Mon Oct 19 2026", r
    assert "Building" not in r["text"] and "ntp clock-period" not in r["text"] and "Last configuration" not in r["text"]
    assert clean_config(ios.replace("1234", "9999").replace("14:22:01", "15:00:00"))["text"] == r["text"]

    nx = "!Command: show running-config\n!Running configuration last done at: Mon Oct 19 14:22:01 2026\n!Time: Tue Oct 20 09:30:01 2026\n\nversion 9.3(8)\nhostname nx1\nfeature bgp\n"
    r = clean_config(nx)
    assert r["problem"] is None and r["changed_by"] is None and r["changed_at"] == "Mon Oct 19 14:22:01 2026", r

    assert clean_config("% Invalid input detected at '^' marker.\n")["problem"]
    assert clean_config("")["problem"]
    assert "cut off" in clean_config("version 15.2\nhostname sw1\ninterface Gi1\n")["problem"]
    assert "hostname" in clean_config("some banner text\nend\n")["problem"]
    print("config_parser self-test passed")