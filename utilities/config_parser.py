"""
config_parser.py - cleans raw `show running-config` output for storage.

clean_config() strips the header lines that change on every pull (so an
unchanged config hashes the same), pulls the device's own "last
configuration change" metadata out of them, and sanity-checks that the
output really is a complete running-config (a denied command or a cut-off
pull must never be stored as a snapshot). Works for IOS/IOS-XE and NX-OS.
"""
import hashlib
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


# ---------------------------------------------------------------------
# Redaction (applied when the viewer pages are generated, never to the
# stored text). Each secret value becomes <redacted:xxxx>, where xxxx is
# the first 4 hex chars of its SHA-256: unreadable, but a CHANGED secret
# still shows up as a change in a diff. Bump REDACTION_VERSION whenever
# the rules change so cached viewer data is rebuilt. Add new patterns to
# _SECRET_RULES: group 1 is the text to keep, group 2 is the secret.
# ---------------------------------------------------------------------
REDACTION_VERSION = 1

_SECRET_RULES = [re.compile(p, re.I) for p in (
    r"^(\s*enable\s+(?:secret|password)(?:\s+level\s+\d+)?(?:\s+\d)?\s+)(\S+)",
    r"^(\s*username\s+\S+\s.*?\b(?:secret|password)(?:\s+\d)?\s+)(\S+)",
    r"^(\s*snmp-server\s+community\s+)(\S+)",
    r"^(\s*snmp-server\s+host\s+\S+(?:\s+(?:traps|informs))?(?:\s+version\s+\S+)?(?:\s+(?:auth|noauth|priv))?\s+)(\S+)",
    r"^(\s*(?:tacacs-server|radius-server)\b.*?\bkey(?:\s+\d)?\s+)(\S+)",
    r"^(\s*key\s+\d\s+)(\S+)\s*$",
    r"^(\s*key\s+)(?!\d+\s*$)(\S+)\s*$",
    r"^(\s*key-string(?:\s+\d)?\s+)(\S+)",
    r"^(\s*crypto\s+isakmp\s+key(?:\s+\d)?\s+)(\S+)",
    r"^(\s*pre-shared-key\b.*?\bkey(?:\s+\d)?\s+)(\S+)",
    r"^(\s*pre-shared-key(?:\s+(?:local|remote))?(?:\s+\d)?\s+)(\S+)\s*$",
    r"^(\s*ip\s+ospf\s+authentication-key(?:\s+\d)?\s+)(\S+)",
    r"^(\s*ip\s+ospf\s+message-digest-key\s+\d+\s+md5(?:\s+\d)?\s+)(\S+)",
    r"^(\s*ip\s+nhrp\s+authentication\s+)(\S+)",
    r"^(\s*ntp\s+authentication-key\s+\d+\s+\S+\s+)(\S+)",
    r"^(\s*neighbor\s+\S+\s+password(?:\s+\d)?\s+)(\S+)",
    r"^(\s*(?:standby|vrrp)\s+\d+\s+authentication\s+(?:md5\s+key-string\s+(?:\d\s+)?|text\s+))(\S+)",
    r"^(\s*vtp\s+password\s+)(\S+)",
    r"^(\s*ip\s+ftp\s+password(?:\s+\d)?\s+)(\S+)",
    r"^(\s*cts\s+credentials\s+id\s+\S+\s+password\s+(?:\d\s+)?)(\S+)",
    r"^(\s*password(?:\s+\d)?\s+)(\S+)\s*$",
    r"^(\s*secret(?:\s+\d)?\s+)(\S+)\s*$",
)]
_SNMP_USER = re.compile(r"^\s*snmp-server\s+user\b", re.I)
_SNMP_USER_SECRET = re.compile(
    r"(\b(?:auth\s+(?:md5|sha\S*)|priv(?:\s+(?:aes(?:-\d+)?|3?des)(?:\s+\d+)?)?)\s+)(\S+)", re.I)
# Cheap pre-filter so the (many) ordinary lines never reach the rule list.
_PRECHECK = re.compile(r"secret|password|community|key|snmp-server (?:user|host)|authentication", re.I)


def _tag(secret: str) -> str:
    if secret.startswith("<redacted:"):
        return secret
    return "<redacted:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:4] + ">"


def _redact_line(ln: str) -> str:
    if _SNMP_USER.match(ln):
        return _SNMP_USER_SECRET.sub(lambda m: m.group(1) + _tag(m.group(2)), ln)
    for rx in _SECRET_RULES:
        m = rx.match(ln)
        if m:
            return ln[:m.start(2)] + _tag(m.group(2)) + ln[m.end(2):]
    return ln


def redact_config(text: str):
    """Returns (redacted_text, number_of_lines_redacted)."""
    out, count = [], 0
    for ln in text.split("\n"):
        if _PRECHECK.search(ln):
            red = _redact_line(ln)
            if red != ln:
                count += 1
                ln = red
        out.append(ln)
    return "\n".join(out), count


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

    cases = {
        "enable secret 9 $9$abcDEF123$xyz": "$9$abcDEF123$xyz",
        "enable password cisco123": "cisco123",
        "username admin privilege 15 secret 9 $9$Zz9$qq": "$9$Zz9$qq",
        "username nxadmin password 5 $5$hashhash role network-admin": "$5$hashhash",
        "snmp-server community Sup3rS3cret RO 10": "Sup3rS3cret",
        "snmp-server host 10.1.1.1 version 2c Sup3rS3cret": "Sup3rS3cret",
        "tacacs-server host 10.2.2.2 key 7 0822455D0A16": "0822455D0A16",
        "radius-server key MyRadiusKey": "MyRadiusKey",
        " key 7 060506324F41": "060506324F41",
        " key-string 7 05080F1C2243": "05080F1C2243",
        "crypto isakmp key VpnKey123 address 1.2.3.4": "VpnKey123",
        " pre-shared-key address 1.2.3.4 key VpnKey456": "VpnKey456",
        " ip ospf authentication-key 7 02050D480809": "02050D480809",
        " ip ospf message-digest-key 1 md5 7 094F471A1A0A": "094F471A1A0A",
        " ip nhrp authentication DmvpnKey": "DmvpnKey",
        "ntp authentication-key 1 md5 104D000A0618 7": "104D000A0618",
        " neighbor 10.0.0.2 password 7 13061E010803": "13061E010803",
        " standby 1 authentication md5 key-string 7 070C285F4D06": "070C285F4D06",
        "vtp password VtpSecret": "VtpSecret",
        "line vty 0 4\n password 7 0822455D0A16": "0822455D0A16",
        "snmp-server user snmpv3 grp v3 auth sha AuthPass123 priv aes 128 PrivPass456": "AuthPass123",
        "snmp-server user u grp auth md5 0xAAAA priv aes-128 0xBBBB localizedkey": "0xBBBB",
    }
    for line, secret in cases.items():
        red, n = redact_config(line)
        assert secret not in red and "<redacted:" in red and n >= 1, (line, red)
    assert "PrivPass456" not in redact_config(list(cases)[20])[0]
    keep = ["key chain KC", " key 1", "  key-string", "hostname sw1", "interface Gi1/0/1", " description uplink key switch",
            " ip ospf authentication message-digest", "snmp-server contact NOC", "tacacs-server timeout 5", "aaa authentication login default group tacacs+ local"]
    
    for line in keep:
        assert redact_config(line)[0] == line, line
    a, _ = redact_config("enable secret 9 $9$same$one"); b, _ = redact_config("enable secret 9 $9$same$one")
    c, _ = redact_config("enable secret 9 $9$diff$one")
    assert a == b and a != c                                  # same secret same tag, changed secret changed tag
    assert redact_config(a)[0] == a                           # idempotent
    print("config_parser self-test passed")