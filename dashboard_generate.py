"""
dashboard_generate.py - Generates the static HTML dashboard (index page
+ one page per site) directly from sad.db. Reuses utilities/db.py for
every query and utilities/topology_layout.py + topology_svg.py for the
per-site topology diagram - no new query or layout logic lives here,
this module is purely "fetch data, fill in HTML."

Usage:
    python3 dashboard_generate.py                  # regenerate everything
    python3 dashboard_generate.py --site <key>      # regenerate one site + the index

Run from the project root, same as orchestrator.py and gui.py.
"""

import sys
import os
import re
import json
import argparse
import datetime
import ipaddress

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utilities"))

import db  # noqa: E402
import topology_layout  # noqa: E402
import topology_svg  # noqa: E402
import mac_parser  # noqa: E402

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")

CSS_TOKENS = """
:root{
  --bg: #0b0f14; --panel: #121821; --panel-2: #182130; --border: #24303f;
  --text: #dbe4ee; --text-dim: #7c8a9a; --accent: #e8a33d; --accent-dim: #7a5a26;
  --up: #3ddc84; --warn: #e85d5d; --radius: 10px;
  --mono: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  --sans: 'Segoe UI', Tahoma, Arial, sans-serif;
}
html[data-theme="light"]{
  --bg: #f2f4f7; --panel: #ffffff; --panel-2: #eef1f5; --border: #dbe1e8;
  --text: #1b2531; --text-dim: #6b7684; --accent: #c07a1e; --accent-dim: #f0d6ac;
  --up: #1f9d5c; --warn: #c0392b;
}
"""

THEME_TOGGLE_JS = """
const root = document.documentElement;
const toggleBtn = document.getElementById("theme-toggle");
const saved = localStorage.getItem("sad-theme");
if(saved){ root.setAttribute("data-theme", saved); toggleBtn.textContent = saved === "dark" ? "\\ud83c\\udf19" : "\\u2600\\ufe0f"; }
toggleBtn.addEventListener("click", () => {
  const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
  root.setAttribute("data-theme", next);
  localStorage.setItem("sad-theme", next);
  toggleBtn.textContent = next === "dark" ? "\\ud83c\\udf19" : "\\u2600\\ufe0f";
});
"""


def _esc(s) -> str:
    return "" if s is None else str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ip_sort_key(ip):
    """Sorts real IPs in genuine numeric order (a plain string sort
    would put 192.20.0.10 before 192.20.0.2, which is wrong) - devices
    with no known IP sort after every device that has one.
    """
    if not ip:
        return (1, 0)
    try:
        return (0, int(ipaddress.ip_address(ip)))
    except ValueError:
        return (1, 0)


def _interface_sort_key(interface):
    """Natural sort for interface names - a plain string sort would
    put Gi1/0/10 before Gi1/0/2, which is wrong. Splits the name into
    alternating text/number chunks (e.g. "Gi1/0/2" -> ["Gi","1","/",
    "0","/","2"]) and zero-pads each numeric chunk to a fixed width
    before joining back into one string - comparing the padded
    strings then produces the same order true numeric comparison
    would, while keeping the whole key a single string throughout (no
    mixed str/int comparison to worry about, regardless of how a real
    device's interface naming happens to be shaped). Different
    prefixes just fall into plain alphabetical order (Fa before Gi
    before Te) without needing any hardcoded notion of interface
    speeds. An interface with no name at all sorts last, matching how
    an unresolved IP already sorts last elsewhere.
    """
    if not interface:
        return (1, "")
    chunks = re.findall(r"\d+|\D+", interface)
    padded = "".join(c.zfill(10) if c.isdigit() else c for c in chunks)
    return (0, padded)


def _fmt_ts(ts) -> str:
    if not ts:
        return "never"
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _is_recent(ts, hours=24) -> bool:
    if not ts:
        return False
    try:
        then = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - then) < datetime.timedelta(hours=hours)


# How long a device/link can go unseen on successive scans before it's
# flagged as stale in the dashboard - a tunable knob, easy to find and
# adjust here without hunting through the rest of the file.
STALE_THRESHOLD_DAYS = 60

# ARP entries churn far faster than physical devices or cabling -
# DHCP leases expire, laptops sleep and grab a new address, phones
# reboot - so they get their own, much shorter threshold rather than
# reusing STALE_THRESHOLD_DAYS. Starting point; tune once there's real
# data to look at, same as the device/link threshold above.
ARP_STALE_THRESHOLD_DAYS = 10

# Clients churn even faster than ARP entries in practice - a laptop
# gets unplugged, a loaner phone moves desks - so this gets the
# shortest threshold of any staleness dimension in the dashboard.
# Automatic timer only, no manual override (unlike devices, which
# needed one specifically because MAC-table collection was brand new
# with no scan history to judge against - that's no longer true here).
CLIENT_STALE_THRESHOLD_DAYS = 3


def _is_stale(last_seen, site_last_cdp_discovery, threshold_days: int = None) -> bool:
    """A device/link/ARP entry is "stale" if it wasn't seen on the
    site's most recent successful scan by at least threshold_days -
    NOT simply "older than N days" by today's date. Comparing against
    today would make every entry at a site look newly stale just
    because the site itself hasn't been rescanned in a while, which
    isn't new information about any specific entry - only a real gap
    between an entry's last_seen and the site's own last scan means
    "this specific thing was missing when we last actually looked."
    No signal at all (site never scanned, or this entry has no
    last_seen) means there's not enough information to judge - never
    flagged stale in that case, matching the same "don't guess" stance
    used for the site-level status dot.

    threshold_days defaults to STALE_THRESHOLD_DAYS (devices/links);
    callers with a different natural churn rate - ARP entries change
    far faster than physical devices or cabling - pass their own
    threshold (see ARP_STALE_THRESHOLD_DAYS) instead of reusing that
    default.
    """
    if threshold_days is None:
        threshold_days = STALE_THRESHOLD_DAYS
    if not last_seen or not site_last_cdp_discovery:
        return False
    try:
        seen_dt = datetime.datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        scan_dt = datetime.datetime.fromisoformat(site_last_cdp_discovery.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (scan_dt - seen_dt) >= datetime.timedelta(days=threshold_days)


def _device_is_stale(device_row, site_last_cdp_discovery) -> bool:
    """A device is stale if EITHER the automatic age-based rule fires
    (_is_stale() above) OR it's been manually flagged via
    marked_stale_at (Site Manager's "Mark Stale" button). The manual
    flag exists specifically for what the automatic rule can't yet
    judge - a deployment too young to have a later rescan to compare
    against (or a site that just hasn't been rescanned in a while)
    means the timer has nothing to detect a gap against yet, even when
    a human already knows from direct knowledge that something's gone.
    """
    return _is_stale(device_row["last_seen"], site_last_cdp_discovery) or device_row["marked_stale_at"] is not None


def _client_is_stale(client_row, site_last_mac_table_collection) -> bool:
    """A client is stale if it wasn't seen on the site's most recent
    MAC-table collection (last_mac_table_collection, NOT
    last_cdp_discovery - the latter advances on every CDP walk
    regardless of whether MAC-table collection was actually requested
    that run, which would make every client at a site look newly
    stale after a plain CDP-only re-scan even though nothing about
    them was actually re-verified). Automatic timer only, unlike
    devices - clients have no manual "marked stale" override.
    """
    return _is_stale(client_row["last_seen"], site_last_mac_table_collection, threshold_days=CLIENT_STALE_THRESHOLD_DAYS)


# ---------------------------------------------------------------------
# Topology data assembly
# ---------------------------------------------------------------------

def build_topology_data(conn, site_id: int, site_last_cdp_discovery=None):
    """Builds the (devices, links) shapes topology_layout.compute_layout()
    expects, straight from sad.db. Includes stub entries for any
    cross-site remote devices referenced by this site's links, since
    the layout/render code needs their hostname + owning site info to
    label the collapsed cross-site group correctly.

    site_last_cdp_discovery: this site's own last successful scan -
    passed through so each local device/link can be flagged "stale"
    against it (see _is_stale()/_device_is_stale()). Cross-site remote
    stub devices are never flagged stale here - they belong to a
    different site's own scan cycle, which this function has no
    visibility into.
    """
    devices = {}
    for d in db.get_devices_for_site(conn, site_id):
        devices[d["id"]] = {
            "id": d["id"], "hostname": d["hostname"], "mgmt_ip": d["mgmt_ip"],
            "arp_override_ip": d["arp_override_ip"], "site_id": d["site_id"],
            "stale": _device_is_stale(d, site_last_cdp_discovery),
        }

    links = [dict(l) for l in conn.execute(
        "SELECT * FROM links WHERE site_id = ?", (site_id,)
    ).fetchall()]

    referenced_ids = {l["device_a_id"] for l in links} | {l["device_b_id"] for l in links}
    missing_ids = referenced_ids - set(devices.keys())
    for device_id in missing_ids:
        row = conn.execute(
            "SELECT d.*, s.site_octet, s.site_name FROM devices d "
            "JOIN sites s ON d.site_id = s.id WHERE d.id = ?",
            (device_id,),
        ).fetchone()
        if row:
            devices[device_id] = {
                "id": row["id"], "hostname": row["hostname"], "mgmt_ip": row["mgmt_ip"],
                "arp_override_ip": row["arp_override_ip"], "site_id": row["site_id"],
                "site_octet": row["site_octet"], "site_name": row["site_name"],
                "stale": False,
            }

    # A link is stale if its own timing looks aged out, OR either
    # endpoint device is itself stale (whether by the automatic timer
    # or a manual "Mark Stale" flag) - computed here against the
    # already-resolved device dict (now that every referenced device,
    # local or cross-site stub, exists in it) rather than stored on
    # the link itself. Nothing to keep in sync: if a device's manual
    # flag is later cleared, every link touching it correctly stops
    # looking stale on the very next generation, with no extra step.
    for link in links:
        a_stale = devices.get(link["device_a_id"], {}).get("stale", False)
        b_stale = devices.get(link["device_b_id"], {}).get("stale", False)
        link["stale"] = _is_stale(link["last_seen"], site_last_cdp_discovery) or a_stale or b_stale

    return devices, links


# ---------------------------------------------------------------------
# Site page
# ---------------------------------------------------------------------

def render_device_list_html(devices_rows, site_last_cdp_discovery) -> str:
    rows_html = []
    for d in devices_rows:
        roles = []
        if d["is_seed"]:
            roles.append('<span class="badge role">CDP seed</span>')
        if d["is_arp_seed"]:
            roles.append('<span class="badge role">ARP seed</span>')
        source_cls = "source-inventory" if d["source"] == "inventory_sync" else "source-cdp"
        source_label = "inventory" if d["source"] == "inventory_sync" else _esc(d["source"])
        stale_badge = ""
        if _device_is_stale(d, site_last_cdp_discovery):
            stale_badge = '<span class="badge stale">stale</span>'
        rows_html.append(f'''
    <div class="row">
      <div class="main">
        <div class="name">{_esc(d["hostname"])}</div>
        <div class="meta">{_esc(d["mgmt_ip"] or "-")} · {_esc(d["platform"] or "unknown platform")}</div>
      </div>
      <div class="badges">
        {"".join(roles)}
        {stale_badge}
        <span class="badge {source_cls}">{source_label}</span>
      </div>
    </div>''')
    return "".join(rows_html) if rows_html else '<div class="row"><div class="main"><div class="meta">No devices found.</div></div></div>'


def render_clients_html(client_rows, devices_by_id, arp_rows, site_last_mac_table_collection, enrichment_rows) -> str:
    """client_rows: db.get_clients_for_site() results. devices_by_id:
    {device_id: hostname} for this site. arp_rows: this site's ARP
    entries - each client's IP (if known) is correlated against these
    via mac_parser.normalize_mac(), not a raw SQL join, since
    clients.mac and arp_entries.mac are independently-reported and
    aren't guaranteed to share the same separator/case format.
    enrichment_rows: db.get_all_phone_enrichment() results (not site-
    scoped - phone_enrichment has no site_id at all, so this is the
    full table each time; simple and correct at the scale this data
    actually reaches, just looked up per-client by normalized MAC).

    Grouped by switch (device_id) rather than one flat list - a large
    site can easily have hundreds of clients, and a single unbroken
    list of that size is unwieldy to scroll even collapsed by default.
    Grouping by switch is a natural, already-meaningful dimension in
    the data (mirrors ARP's own per-VRF grouping, reusing the exact
    same .vrf-group markup/CSS - no new styling needed) and keeps each
    group's size naturally bounded by that switch's own port count.
    """
    if not client_rows:
        return (
            '<p style="padding:14px; font-family:var(--mono); font-size:12px; color:var(--text-dim);">'
            'No client data collected yet - run CDP discovery with "Also collect MAC address tables" enabled.</p>'
        )

    ip_by_mac = {}
    for arp in arp_rows:
        norm = mac_parser.normalize_mac(arp["mac"])
        ips = ip_by_mac.setdefault(norm, [])
        if arp["ip"] not in ips:
            ips.append(arp["ip"])

    enrichment_by_mac = {row["mac"]: row for row in enrichment_rows}

    def sort_key(c):
        ips = ip_by_mac.get(mac_parser.normalize_mac(c["mac"]), [])
        ip_key = _ip_sort_key(ips[0] if ips else None)
        return (_interface_sort_key(c["interface"]), ip_key)

    def render_row(c):
        ips = ip_by_mac.get(mac_parser.normalize_mac(c["mac"]), [])
        ip_label = ", ".join(ips) if ips else "-"
        vlan_label = c["vlan"] or "-"
        norm_mac = mac_parser.normalize_mac(c["mac"])
        enrichment = enrichment_by_mac.get(norm_mac)

        badges = []
        type_label = {"cisco_phone": "phone", "vtc": "VTC"}.get(c["device_type"])
        if type_label:
            if enrichment and enrichment["model"]:
                type_label = f"{type_label} \u00b7 {enrichment['model']}"
            badges.append(f'<span class="badge role">{_esc(type_label)}</span>')
        if _client_is_stale(c, site_last_mac_table_collection):
            badges.append('<span class="badge stale">stale</span>')

        has_expand = enrichment is not None and any(
            [enrichment["phone_number"], enrichment["serial_number"], enrichment["ris_status"], enrichment["ris_ip"]]
        )
        panel_id = f"client-{norm_mac}"
        row_attrs = f' onclick="toggleExpand(\'{panel_id}\')" style="cursor:pointer;"' if has_expand else ""

        row_html = f'''
    <div class="row"{row_attrs}>
      <div class="main client-row-main">
        <span class="name">{_esc(c["interface"] or "-")}</span>
        <span class="meta">VLAN {_esc(vlan_label)} \u00b7 {_esc(ip_label)} \u00b7 {_esc(c["mac"])}</span>
      </div>
      <div class="badges">
        {"".join(badges)}
      </div>
    </div>'''

        if has_expand:
            row_html += f'''
    <div id="{panel_id}" class="remote-list" hidden>
      <div class="remote-list-title">CUCM / RIS Details</div>
      <div class="enrich-row"><span>Phone number(s)</span><span>{_esc(enrichment["phone_number"] or "-")}</span></div>
      <div class="enrich-row"><span>Serial</span><span>{_esc(enrichment["serial_number"] or "-")}</span></div>
      <div class="enrich-row"><span>RIS status</span><span>{_esc(enrichment["ris_status"] or "-")}</span></div>
      <div class="enrich-row"><span>RIS IP</span><span>{_esc(enrichment["ris_ip"] or "-")}</span></div>
      <div class="enrich-row"><span>Last enriched</span><span>{_fmt_ts(enrichment["last_enriched"])}</span></div>
    </div>'''

        return row_html

    groups = {}
    for c in client_rows:
        groups.setdefault(c["device_id"], []).append(c)

    html_parts = []
    for device_id, entries in sorted(groups.items(), key=lambda kv: devices_by_id.get(kv[0], "")):
        switch_name = devices_by_id.get(device_id, "(unknown device)")
        rows_html = "".join(render_row(c) for c in sorted(entries, key=sort_key))
        open_attr = "" if len(entries) > ARP_GROUP_AUTO_COLLAPSE_THRESHOLD else "open"
        html_parts.append(f'''
  <details class="vrf-group" {open_attr}>
    <summary class="vrf-group-head"><span class="vrf-name"><span class="chevron">\u25b8</span>{_esc(switch_name)}</span><span>{len(entries)} clients</span></summary>
    {rows_html}
  </details>''')
    return f'<div class="vrf-groups">{"".join(html_parts)}</div>'


ARP_GROUP_AUTO_COLLAPSE_THRESHOLD = 15


def _render_vrf_groups_html(rows) -> str:
    """Groups ARP rows by VRF and renders each as its own collapsible
    panel (auto-collapsed past ARP_GROUP_AUTO_COLLAPSE_THRESHOLD
    entries, same as always). Shared by both the active and stale
    sections in render_arp_tables_html() below - the only difference
    between them is which subset of rows gets passed in and what wraps
    the result, not how a single VRF's own table gets built. Returns
    "" if rows is empty, so callers can decide what (if anything) to
    show in that case.
    """
    groups = {}
    for row in rows:
        groups.setdefault(row["vrf"], []).append(row)
    if not groups:
        return ""

    html_parts = []
    for vrf, entries in sorted(groups.items(), key=lambda kv: (kv[0] != "", kv[0])):
        vrf_label = "(global)" if vrf == "" else _esc(vrf)
        # Interface/age aren't stored as their own columns on arp_entries
        # (only ip/mac/vrf/source/raw are) - pull them back out of the
        # stored raw_source_data line for display instead of adding
        # columns that would need a schema change for a cosmetic need.
        rows_html = "".join(_render_arp_row(e) for e in entries)
        open_attr = "" if len(entries) > ARP_GROUP_AUTO_COLLAPSE_THRESHOLD else "open"
        html_parts.append(f'''
  <details class="vrf-group" {open_attr}>
    <summary class="vrf-group-head"><span class="vrf-name"><span class="chevron">\u25b8</span>{vrf_label}</span><span>{len(entries)} entries</span></summary>
    <table>
      <thead><tr><th>IP</th><th>MAC</th><th>Interface</th><th>Age</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </details>''')
    return "".join(html_parts)


def render_arp_tables_html(arp_rows, site_last_arp_collection) -> str:
    """Active and stale entries are split FIRST, then grouped by VRF
    independently within each - not nested the other way around - so
    a VRF with both active and stale entries shows up in both
    sections rather than making someone hunt through an otherwise-
    normal-looking VRF table for the stale rows mixed in among it.
    A VRF that has only active (or only stale) entries only appears
    in the relevant section, never as an empty entry in the other.
    """
    if not arp_rows:
        return '<p style="padding:14px; font-family:var(--mono); font-size:12px; color:var(--text-dim);">No ARP data collected yet.</p>'

    active_rows, stale_rows = [], []
    for row in arp_rows:
        if _is_stale(row["last_seen"], site_last_arp_collection, threshold_days=ARP_STALE_THRESHOLD_DAYS):
            stale_rows.append(row)
        else:
            active_rows.append(row)

    active_html = _render_vrf_groups_html(active_rows)
    if not active_html:
        active_html = '<p style="padding:0 14px 14px; font-family:var(--mono); font-size:12px; color:var(--text-dim);">No active entries.</p>'

    parts = [f'<div class="vrf-groups"><div class="arp-section-label">Active ARP Entries</div>{active_html}']

    if stale_rows:
        stale_html = _render_vrf_groups_html(stale_rows)
        parts.append(f'''
  <details class="arp-stale-section">
    <summary class="stale-section-head"><span class="chevron">\u25b8</span>Stale ARP Entries ({len(stale_rows)} entries)</summary>
    {stale_html}
  </details>''')

    parts.append('</div>')
    return "".join(parts)


def _render_arp_row(entry) -> str:
    """arp_entries doesn't have its own interface/age columns - both
    parsers already stash the full original line in raw_source_data,
    so pull them back out for display rather than changing the schema
    just for a cosmetic column.
    """
    interface, age = "", ""
    raw = entry["raw_source_data"] or ""
    parts = raw.split()
    if parts:
        # IOS: Internet <ip> <age> <mac> <type> <interface>
        # NX-OS: <ip> <age> <mac> <interface> [flags]
        if parts[0].upper() == "INTERNET" and len(parts) == 6:
            age, interface = parts[2], parts[5]
        elif len(parts) in (4, 5):
            age, interface = parts[1], parts[3]
    return (
        f'<tr><td title="{_esc(entry["ip"])}">{_esc(entry["ip"])}</td>'
        f'<td title="{_esc(entry["mac"])}">{_esc(entry["mac"])}</td>'
        f'<td title="{_esc(interface)}">{_esc(interface)}</td>'
        f'<td title="{_esc(age)}">{_esc(age)}</td></tr>'
    )


def render_site_page(conn, site_row) -> str:
    site_id = site_row["id"]
    devices_rows = sorted(db.get_devices_for_site(conn, site_id), key=lambda d: _ip_sort_key(d["mgmt_ip"]))
    arp_rows = db.get_arp_for_site(conn, site_id)
    client_rows = db.get_clients_for_site(conn, site_id)
    seed = db.get_seed_device_for_site(conn, site_id)
    arp_seed = db.get_arp_seed_device_for_site(conn, site_id)

    devices, links = build_topology_data(conn, site_id, site_row["last_cdp_discovery"])
    topology_html = '<p style="padding:14px; font-family:var(--mono); font-size:12px; color:var(--text-dim);">No seed device flagged - nothing to render.</p>'
    expand_panels_html = ""
    device_count_label = f"{len(devices_rows)} devices"
    if seed is not None and seed["id"] in devices:
        layout = topology_layout.compute_layout(seed["id"], devices, links, site_id)
        result = topology_svg.render_svg(layout, devices)
        topology_html = result["svg"]
        panels = []
        for p in result["expand_panels"]:
            items = "".join(
                f'<a class="remote-item" href="#">{_esc(it["label"])} <span class="site-tag">{_esc(it["sub"])}</span></a>'
                for it in p["items"]
            )
            panels.append(f'''
    <div id="{p["id"]}" class="remote-list" hidden>
      <div class="remote-list-title">{_esc(p["title"])}</div>
      {items}
    </div>''')
        expand_panels_html = "".join(panels)
        device_count_label = f"{len(devices_rows)} devices shown"

    site_label = site_row["site_name"] or f"octet {site_row['site_octet']}"
    code_label = f" — {_esc(site_row['site_code'])}" if site_row["site_code"] else ""
    seed_label = seed["hostname"] if seed else "(none flagged)"
    arp_seed_label = arp_seed["hostname"] if arp_seed else "(none flagged)"
    arp_override_note = ""
    if arp_seed and arp_seed["arp_override_ip"]:
        arp_override_note = f" (override \u2192 {_esc(arp_seed['arp_override_ip'])})"

    total_arp = len(arp_rows)
    vrf_count = len({row["vrf"] for row in arp_rows}) if arp_rows else 0

    return f'''<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(site_label)} ({_esc(site_row['site_octet'])}) — Site Awareness Dashboard</title>
<style>
{CSS_TOKENS}
*{{ box-sizing: border-box; }}
body{{ margin:0; background:var(--bg); color:var(--text); font-family:var(--sans); -webkit-font-smoothing:antialiased; }}
.wrap{{ max-width:1200px; margin:0 auto; padding:24px 24px 60px; }}
.backlink{{ display:inline-block; font-family:var(--mono); font-size:12px; color:var(--text-dim); border:1px solid var(--border); border-radius:999px; padding:5px 12px; text-decoration:none; margin-bottom:14px; }}
.backlink:hover{{ color:var(--accent); border-color:var(--accent); }}
header{{ display:flex; justify-content:space-between; align-items:flex-end; border-bottom:2px solid var(--border); padding-bottom:16px; margin-bottom:20px; gap:16px; flex-wrap:wrap; }}
header h1{{ margin:0; font-size:22px; font-weight:700; }}
.subtitle{{ font-family:var(--mono); font-size:12px; color:var(--text-dim); margin-top:4px; }}
button, .btn{{ background:var(--panel-2); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:var(--sans); font-size:13px; padding:8px 14px; cursor:pointer; transition:border-color .15s ease, transform .1s ease; text-decoration:none; display:inline-block; }}
button:hover, .btn:hover{{ border-color:var(--accent); }}
button:active, .btn:active{{ transform:scale(.97); }}
.header-actions{{ display:flex; gap:8px; align-items:center; }}
.icon-btn{{ width:38px; height:38px; padding:0; display:flex; align-items:center; justify-content:center; font-size:16px; }}
.panel{{ background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; margin-bottom:18px; }}
.panel-head{{ background:var(--panel-2); border-bottom:1px solid var(--border); padding:8px 14px; display:flex; justify-content:space-between; align-items:center; cursor:pointer; list-style:none; }}
.panel-head::-webkit-details-marker{{ display:none; }}
.panel-head .label{{ font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; display:flex; align-items:center; }}
.panel-head .count{{ font-family:var(--mono); font-size:11px; background:var(--bg); border:1px solid var(--border); border-radius:999px; padding:2px 9px; color:var(--text-dim); }}
.chevron{{ display:inline-block; margin-right:8px; color:var(--text-dim); font-size:10px; transition:transform .15s ease; transform:rotate(90deg); }}
details:not([open]) > .panel-head .chevron, details:not([open]) > .vrf-group-head .chevron, details:not([open]) > .stale-section-head .chevron{{ transform:rotate(0deg); }}
.grouped-node{{ cursor:pointer; }}
.grouped-node:hover .node-box.grouped, .grouped-node:hover .node-box{{ stroke:var(--accent); }}
.remote-list{{ margin:14px 16px 14px; border:1px solid var(--accent-dim); border-radius:8px; background:var(--panel-2); overflow:hidden; }}
.remote-list-title{{ font-family:var(--mono); font-size:10.5px; color:var(--text-dim); padding:8px 12px; border-bottom:1px solid var(--border); text-transform:uppercase; letter-spacing:.05em; }}
.remote-item{{ display:block; font-family:var(--mono); font-size:12px; color:var(--text); text-decoration:none; padding:7px 12px; border-bottom:1px solid var(--border); }}
.remote-item:last-child{{ border-bottom:none; }}
.remote-item:hover{{ background:var(--panel); color:var(--accent); }}
.remote-item .site-tag{{ color:var(--text-dim); }}
.enrich-row{{ display:flex; justify-content:space-between; gap:12px; font-family:var(--mono); font-size:12px; padding:7px 12px; border-bottom:1px solid var(--border); }}
.enrich-row:last-child{{ border-bottom:none; }}
.enrich-row span:first-child{{ color:var(--text-dim); }}
#topology{{ width:100%; height:auto; display:block; }}
.node-box{{ fill:var(--panel-2); stroke:var(--border); stroke-width:1; }}
.node-box.grouped{{ fill:var(--bg); stroke:var(--accent-dim); stroke-dasharray:3 3; }}
.node-box.stale{{ opacity:0.5; stroke-dasharray:2 2; }}
.node-label{{ font-family:var(--mono); font-size:11px; fill:var(--text); }}
.node-sub{{ font-family:var(--mono); font-size:9px; fill:var(--text-dim); }}
.link-line{{ stroke:var(--text-dim); stroke-width:1.3; transition:stroke .1s ease, stroke-width .1s ease; }}
.link-line.grouped{{ stroke:var(--accent-dim); stroke-width:1.3; stroke-dasharray:4 3; }}
.link-line.stale{{ opacity:0.45; stroke-dasharray:1 3; }}
.edge:hover .link-line{{ stroke:var(--accent); stroke-width:2.2; }}
.row{{ display:flex; align-items:center; gap:14px; padding:9px 14px; border-bottom:1px solid var(--border); }}
.row:last-child{{ border-bottom:none; }}
.row:hover{{ background:var(--panel-2); }}
.row .main{{ flex:1; min-width:0; }}
.row .name{{ font-size:14px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.row .meta{{ font-family:var(--mono); font-size:11.5px; color:var(--text-dim); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.client-row-main{{ display:flex; align-items:baseline; gap:10px; }}
.client-row-main .meta{{ margin-top:0; }}
.badges{{ display:flex; gap:6px; flex:none; }}
.badge{{ font-family:var(--mono); font-size:10.5px; border-radius:999px; padding:2px 9px; white-space:nowrap; border:1px solid var(--border); }}
.badge.source-inventory{{ color:var(--up); border-color:color-mix(in srgb, var(--up) 40%, var(--border)); }}
.badge.source-cdp{{ color:var(--text-dim); }}
.badge.source-manual{{ color:var(--accent); border-color:var(--accent-dim); }}
.badge.role{{ color:var(--accent); border-color:var(--accent-dim); }}
.badge.stale{{ color:var(--text-dim); border-style:dashed; }}
.vrf-groups{{ padding:14px; }}
.vrf-group{{ border:1px solid var(--border); border-radius:8px; overflow:hidden; margin-bottom:14px; display:block; }}
.vrf-group-head{{ background:var(--panel-2); padding:9px 14px; font-family:var(--mono); font-size:13px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--text); display:flex; justify-content:space-between; cursor:pointer; list-style:none; }}
.vrf-group-head::-webkit-details-marker{{ display:none; }}
.vrf-group-head .vrf-name{{ display:flex; align-items:center; }}
.arp-section-label{{ font-family:var(--mono); font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--text); padding:14px 14px 4px; }}
.arp-stale-section{{ margin:0 14px 14px; }}
.stale-section-head{{ font-family:var(--mono); font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--text-dim); cursor:pointer; padding:8px 0; list-style:none; display:flex; align-items:center; }}
.stale-section-head::-webkit-details-marker{{ display:none; }}
.stale-section-head:hover{{ color:var(--accent); }}
table{{ width:100%; table-layout:fixed; border-collapse:collapse; font-family:var(--mono); font-size:12px; }}
th:nth-child(1), td:nth-child(1){{ width:26%; }}
th:nth-child(2), td:nth-child(2){{ width:30%; }}
th:nth-child(3), td:nth-child(3){{ width:30%; }}
th:nth-child(4), td:nth-child(4){{ width:14%; }}
th, td{{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
th{{ text-align:left; padding:6px 12px; color:var(--text-dim); font-weight:600; border-bottom:1px solid var(--border); font-size:10.5px; text-transform:uppercase; letter-spacing:.05em; }}
td{{ padding:5px 12px; border-bottom:1px solid var(--border); }}
tr:last-child td{{ border-bottom:none; }}
tr:hover td{{ background:var(--panel-2); }}
footer{{ margin-top:20px; font-family:var(--mono); font-size:11px; color:var(--text-dim); text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="backlink" href="index.html">\u2190 All Sites</a>
  <header>
    <div>
      <h1>{_esc(site_label)}{code_label}</h1>
      <div class="subtitle">octet {_esc(site_row['site_octet'])} · CDP seed: {_esc(seed_label)} · ARP seed: {_esc(arp_seed_label)}{arp_override_note} · last CDP {_fmt_ts(site_row['last_cdp_discovery'])} · last ARP {_fmt_ts(site_row['last_arp_collection'])}</div>
    </div>
    <div class="header-actions">
      <button class="icon-btn" id="theme-toggle" title="Toggle theme">\U0001F319</button>
    </div>
  </header>

  <details class="panel" open>
    <summary class="panel-head"><span class="label"><span class="chevron">\u25b8</span>Topology</span><span class="count">{device_count_label}</span></summary>
    {topology_html}
    {expand_panels_html}
  </details>

  <details class="panel" open>
    <summary class="panel-head"><span class="label"><span class="chevron">\u25b8</span>Devices</span><span class="count">{len(devices_rows)} total</span></summary>
    <div>{render_device_list_html(devices_rows, site_row["last_cdp_discovery"])}</div>
  </details>

  <details class="panel" open>
    <summary class="panel-head"><span class="label"><span class="chevron">\u25b8</span>ARP Tables</span><span class="count">{vrf_count} tables · {total_arp} entries</span></summary>
    {render_arp_tables_html(arp_rows, site_row["last_arp_collection"])}
  </details>

  <details class="panel">
    <summary class="panel-head"><span class="label"><span class="chevron">\u25b8</span>Clients</span><span class="count">{len(client_rows)} total</span></summary>
    <div>{render_clients_html(client_rows, {d["id"]: d["hostname"] for d in devices_rows}, arp_rows, site_row["last_mac_table_collection"], db.get_all_phone_enrichment(conn))}</div>
  </details>

  <footer>{_esc(site_label)} (octet {_esc(site_row['site_octet'])}) — generated from sad.db, read-only</footer>
</div>
<script>
function toggleExpand(id){{
  const el = document.getElementById(id);
  el.hidden = !el.hidden;
}}
{THEME_TOGGLE_JS}
</script>
</body>
</html>
'''


# ---------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------

def render_index_page(conn) -> str:
    sites = db.get_all_sites(conn)
    # "Unassigned" is a real row in the sites table (so the generator
    # can produce a normal site-unassigned.html page for it) but isn't
    # a real site - it shouldn't be counted or listed alongside actual
    # sites. It gets its own dedicated header button/page instead (see
    # unassigned_count below).
    real_sites = [s for s in sites if s["site_octet"] != "unassigned"]
    unassigned_site = next((s for s in sites if s["site_octet"] == "unassigned"), None)

    total_devices = conn.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"]
    total_links = conn.execute("SELECT COUNT(*) AS c FROM links").fetchone()["c"]
    total_clients = conn.execute("SELECT COUNT(*) AS c FROM clients").fetchone()["c"]
    never_scanned = sum(1 for s in real_sites if not s["last_cdp_discovery"])

    unassigned_count = 0
    if unassigned_site is not None:
        unassigned_count = conn.execute(
            "SELECT COUNT(*) AS c FROM devices WHERE site_id = ?", (unassigned_site["id"],)
        ).fetchone()["c"]

    # Only counted for real sites - Unassigned has no last_cdp_discovery
    # to compare against (it's never CDP-scanned), so nothing there can
    # ever be judged stale by this rule.
    stale_device_count = 0
    for s in real_sites:
        if not s["last_cdp_discovery"]:
            continue
        site_devices = conn.execute(
            "SELECT last_seen, marked_stale_at FROM devices WHERE site_id = ?", (s["id"],)
        ).fetchall()
        stale_device_count += sum(1 for d in site_devices if _device_is_stale(d, s["last_cdp_discovery"]))

    rows_html = []
    for s in real_sites:
        device_count = conn.execute(
            "SELECT COUNT(*) AS c FROM devices WHERE site_id = ?", (s["id"],)
        ).fetchone()["c"]
        link_count = conn.execute(
            "SELECT COUNT(*) AS c FROM links WHERE site_id = ?", (s["id"],)
        ).fetchone()["c"]

        label = s["site_name"] or f"(unset — octet {s['site_octet']})"
        code_meta = f" · {_esc(s['site_code'])}" if s["site_code"] else ""

        if not s["last_cdp_discovery"]:
            dot_class = ""
        elif _is_recent(s["last_cdp_discovery"]):
            dot_class = "up"
        else:
            dot_class = "stale"

        href = f"site-{s['site_octet']}.html"

        rows_html.append(f'''
    <a class="row" href="{href}">
      <span class="status-dot {dot_class}"></span>
      <div class="main">
        <div class="name">{_esc(label)}</div>
        <div class="meta">octet {_esc(s['site_octet'])}{code_meta}</div>
      </div>
      <div class="counts">
        <span class="pill">{device_count} dev</span>
        <span class="pill">{link_count} links</span>
      </div>
      <div class="runinfo">cdp {_fmt_ts(s['last_cdp_discovery'])}<br>arp {_fmt_ts(s['last_arp_collection'])}</div>
    </a>''')

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f'''<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Site Awareness Dashboard</title>
<style>
{CSS_TOKENS}
*{{ box-sizing: border-box; }}
body{{ margin:0; background:var(--bg); color:var(--text); font-family:var(--sans); -webkit-font-smoothing:antialiased; }}
.wrap{{ max-width:1200px; margin:0 auto; padding:24px 24px 60px; }}
header{{ display:flex; justify-content:space-between; align-items:flex-end; border-bottom:2px solid var(--border); padding-bottom:16px; margin-bottom:20px; gap:16px; flex-wrap:wrap; }}
header h1{{ margin:0; font-size:22px; font-weight:700; }}
.subtitle{{ font-family:var(--mono); font-size:12px; color:var(--text-dim); margin-top:4px; }}
.header-actions{{ display:flex; gap:8px; align-items:center; }}
button, .btn{{ background:var(--panel-2); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:var(--sans); font-size:13px; padding:8px 14px; cursor:pointer; transition:border-color .15s ease, transform .1s ease; text-decoration:none; display:inline-block; }}
button:hover, .btn:hover{{ border-color:var(--accent); }}
button:active, .btn:active{{ transform:scale(.97); }}
.icon-btn{{ width:38px; height:38px; padding:0; display:flex; align-items:center; justify-content:center; font-size:16px; }}
.kpi-grid{{ display:grid; grid-template-columns:repeat(auto-fill, minmax(175px,1fr)); gap:12px; margin-bottom:24px; }}
.kpi{{ background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); padding:14px 16px; }}
.kpi .num{{ font-family:var(--mono); font-size:26px; font-weight:700; }}
.kpi .num.warn{{ color:var(--warn); }}
.kpi .label{{ font-family:var(--mono); font-size:11px; text-transform:uppercase; letter-spacing:.07em; color:var(--text-dim); margin-top:4px; }}
.search-row{{ margin-bottom:14px; }}
#search{{ width:100%; background:var(--panel); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:var(--mono); font-size:13px; padding:10px 14px; }}
#search::placeholder{{ color:var(--text-dim); }}
#search:focus{{ outline:none; border-color:var(--accent); }}
.panel{{ background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }}
.panel-head{{ background:var(--panel-2); border-bottom:1px solid var(--border); padding:8px 14px; display:flex; justify-content:space-between; align-items:center; }}
.panel-head .label{{ font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; }}
.panel-head .count{{ font-family:var(--mono); font-size:11px; background:var(--bg); border:1px solid var(--border); border-radius:999px; padding:2px 9px; color:var(--text-dim); }}
.row{{ display:flex; align-items:center; gap:14px; padding:10px 14px; border-bottom:1px solid var(--border); cursor:pointer; text-decoration:none; color:inherit; }}
.row:last-child{{ border-bottom:none; }}
.row:hover{{ background:var(--panel-2); }}
.status-dot{{ width:8px; height:8px; border-radius:50%; background:var(--text-dim); flex:none; transition:box-shadow .15s ease; }}
.status-dot.up{{ background:var(--up); box-shadow:0 0 0 4px color-mix(in srgb, var(--up) 20%, transparent); }}
.status-dot.stale{{ background:var(--warn); }}
.row .main{{ flex:1; min-width:0; }}
.row .name{{ font-size:14.5px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.row .meta{{ font-family:var(--mono); font-size:11.5px; color:var(--text-dim); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.row .counts{{ display:flex; gap:6px; flex:none; }}
.pill{{ font-family:var(--mono); font-size:11px; border:1px solid var(--border); border-radius:999px; padding:2px 9px; color:var(--text-dim); white-space:nowrap; }}
.pill.needs-review{{ border-color:var(--warn); color:var(--warn); }}
.row .runinfo{{ font-family:var(--mono); font-size:11px; color:var(--text-dim); text-align:right; flex:none; width:150px; }}
footer{{ margin-top:20px; font-family:var(--mono); font-size:11px; color:var(--text-dim); text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Site Awareness Dashboard</h1>
      <div class="subtitle">{len(real_sites)} sites · {total_devices} devices tracked · generated {generated_at}</div>
    </div>
    <div class="header-actions">
      <a class="btn" href="clients.html">Search Clients</a>
      <a class="btn" href="exports.html">Exports</a>
      <a class="btn" href="site-unassigned.html">Unassigned ({unassigned_count})</a>
      <button class="icon-btn" id="theme-toggle" title="Toggle theme">\U0001F319</button>
    </div>
  </header>

  <div class="kpi-grid">
    <div class="kpi"><center><div class="num">{len(real_sites)}</div><div class="label">Sites</div></center></div>
    <div class="kpi"><center><div class="num">{total_devices}</div><div class="label">Devices</div></center></div>
    <div class="kpi"><center><div class="num">{total_links}</div><div class="label">Links</div></center></div>
    <div class="kpi"><center><div class="num">{total_clients}</div><div class="label">Clients</div></center></div>
    <div class="kpi"><center>{'<div class="num warn">' if never_scanned > 0 else '<div class="num">'}{never_scanned}</div><div class="label">Never CDP-scanned</div></center></div>
    <div class="kpi"><center><div class="num warn">{stale_device_count}</div><div class="label">Stale devices</div></center></div>
  </div>

  <div class="search-row">
    <input id="search" type="text" placeholder="Filter by name, octet, or code…" autocomplete="off">
  </div>

  <div class="panel">
    <div class="panel-head"><span class="label">Sites</span><span class="count" id="visible-count">{len(real_sites)} shown</span></div>
    <div id="site-list">{"".join(rows_html)}</div>
  </div>

  <footer>Site Awareness Dashboard — generated from sad.db, read-only</footer>
</div>
<script>
document.getElementById("search").addEventListener("input", (e) => {{
  const f = e.target.value.trim().toLowerCase();
  const rows = document.querySelectorAll("#site-list .row");
  let visible = 0;
  rows.forEach(r => {{
    const match = !f || r.textContent.toLowerCase().includes(f);
    r.style.display = match ? "" : "none";
    if(match) visible++;
  }});
  document.getElementById("visible-count").textContent = `${{visible}} shown`;
}});
{THEME_TOGGLE_JS}
</script>
</body>
</html>
'''


# ---------------------------------------------------------------------
# Generation entry points
# ---------------------------------------------------------------------

CLIENT_SEARCH_MIN_QUERY_LENGTH = 4
CLIENT_SEARCH_MAX_RESULTS = 200


def _gather_all_clients_for_search(conn):
    """Pulls every client across every real site (Unassigned excluded,
    same as everywhere else on the index page) into one flat list for
    the standalone cross-site client search page. Each client's IP is
    correlated the same way render_clients_html() does per-site (via
    mac_parser.normalize_mac() against that site's own arp_entries,
    not a raw SQL join) - genuinely re-querying per site rather than
    reusing anything cached, since this needs to reflect every site's
    current data, not just whichever one/few pages happen to be
    regenerating right now.

    Includes model (from phone_enrichment, if any) alongside type, so
    the search page's badge can show the same "phone · CP-8865NR"
    detail the site pages do. Deliberately does NOT include serial/
    RIS status/RIS IP here - unlike the site page's plain rows, a
    search result row is already a link to its own site page, so
    adding a click-to-expand interaction on top of that would conflict
    with just clicking through; the fuller CUCM/RIS detail stays a
    site-page-only feature for that reason.
    """
    real_sites = [s for s in db.get_all_sites(conn) if s["site_octet"] != "unassigned"]
    enrichment_by_mac = {row["mac"]: row for row in db.get_all_phone_enrichment(conn)}

    results = []
    for site in real_sites:
        site_id = site["id"]
        client_rows = db.get_clients_for_site(conn, site_id)
        if not client_rows:
            continue
        arp_rows = db.get_arp_for_site(conn, site_id)
        devices_by_id = {d["id"]: d["hostname"] for d in db.get_devices_for_site(conn, site_id)}

        ip_by_mac = {}
        for arp in arp_rows:
            norm = mac_parser.normalize_mac(arp["mac"])
            ips = ip_by_mac.setdefault(norm, [])
            if arp["ip"] not in ips:
                ips.append(arp["ip"])

        for c in client_rows:
            ips = ip_by_mac.get(mac_parser.normalize_mac(c["mac"]), [])
            enrichment = enrichment_by_mac.get(mac_parser.normalize_mac(c["mac"]))
            results.append({
                "mac": c["mac"],
                "site": site["site_octet"],
                "siteName": site["site_name"] or "",
                "switch": devices_by_id.get(c["device_id"], ""),
                "iface": c["interface"] or "",
                "vlan": c["vlan"] or "",
                "ip": ", ".join(ips),
                "type": c["device_type"] or "",
                "model": (enrichment["model"] if enrichment else None) or "",
                "serial": (enrichment["serial_number"] if enrichment else None) or "",
                "risStatus": (enrichment["ris_status"] if enrichment else None) or "",
                "risIp": (enrichment["ris_ip"] if enrichment else None) or "",
                "phoneNumber": (enrichment["phone_number"] if enrichment else None) or "",
                "stale": _client_is_stale(c, site["last_mac_table_collection"]),
            })
    return results


def render_client_search_page(conn) -> str:
    clients = _gather_all_clients_for_search(conn)
    clients_json = json.dumps(clients)
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f'''<!DOCTYPE html>
<html data-theme="light">
<head>
<meta charset="UTF-8">
<title>Client Search \u2014 Site Awareness Dashboard</title>
<style>
{CSS_TOKENS}
body{{ margin:0; background:var(--bg); color:var(--text); font-family:var(--sans); }}
.wrap{{ max-width:1100px; margin:0 auto; padding:24px; }}
a{{ color:inherit; }}
button, .btn{{ background:var(--panel-2); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:var(--sans); font-size:13px; padding:8px 14px; cursor:pointer; transition:border-color .15s ease, transform .1s ease; text-decoration:none; display:inline-block; }}
button:hover, .btn:hover{{ border-color:var(--accent); }}
button:active, .btn:active{{ transform:scale(.97); }}
.header-actions{{ display:flex; gap:8px; align-items:center; }}
.icon-btn{{ width:38px; height:38px; padding:0; display:flex; align-items:center; justify-content:center; font-size:16px; }}
header{{ display:flex; justify-content:space-between; align-items:flex-start; padding-bottom:16px; border-bottom:1px solid var(--border); margin-bottom:20px; }}
h1{{ font-size:22px; margin:0 0 4px; }}
.subtitle{{ font-family:var(--mono); font-size:12px; color:var(--text-dim); }}
.backlink{{ display:inline-block; font-family:var(--mono); font-size:12px; color:var(--text-dim); border:1px solid var(--border); border-radius:999px; padding:5px 12px; text-decoration:none; margin-bottom:14px; }}
.backlink:hover{{ color:var(--accent); border-color:var(--accent); }}
.search-row{{ margin-bottom:10px; }}
#search{{ width:100%; box-sizing:border-box; background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:12px 14px; font-family:var(--mono); font-size:14px; color:var(--text); }}
#search:focus{{ outline:none; border-color:var(--accent); }}
#status{{ font-family:var(--mono); font-size:12px; color:var(--text-dim); margin-bottom:14px; }}
.row{{ display:flex; align-items:stretch; border-bottom:1px solid var(--border); background:var(--panel); }}
.row:first-child{{ border-radius:8px 8px 0 0; overflow:hidden; }}
.row:last-child{{ border-bottom:none; border-radius:0 0 8px 8px; overflow:hidden; }}
.row-main{{ flex:1; min-width:0; display:flex; align-items:center; gap:14px; padding:10px 14px; }}
.row-main:hover{{ background:var(--panel-2); }}
.row-link{{ display:flex; align-items:center; padding:0 16px; color:var(--text-dim); text-decoration:none; border-left:1px solid var(--border); font-size:16px; flex:none; }}
.row-link:hover{{ color:var(--accent); background:var(--panel-2); }}
.main{{ flex:1; min-width:0; display:flex; align-items:baseline; gap:10px; }}
.name{{ font-size:14px; font-weight:600; white-space:nowrap; }}
.meta{{ font-family:var(--mono); font-size:11.5px; color:var(--text-dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.match{{ background:var(--accent-dim); color:var(--text); border-radius:3px; padding:0 2px; }}
.badge{{ font-family:var(--mono); font-size:10.5px; border-radius:999px; padding:2px 9px; white-space:nowrap; border:1px solid var(--border); color:var(--accent); border-color:var(--accent-dim); flex:none; }}
.badge.stale{{ color:var(--text-dim); border-color:var(--border); border-style:dashed; }}
.remote-list{{ margin:0 16px 14px; border:1px solid var(--accent-dim); border-radius:8px; background:var(--panel-2); overflow:hidden; }}
.remote-list-title{{ font-family:var(--mono); font-size:10.5px; color:var(--text-dim); padding:8px 12px; border-bottom:1px solid var(--border); text-transform:uppercase; letter-spacing:.05em; }}
.enrich-row{{ display:flex; justify-content:space-between; gap:12px; font-family:var(--mono); font-size:12px; padding:7px 12px; border-bottom:1px solid var(--border); }}
.enrich-row:last-child{{ border-bottom:none; }}
.enrich-row span:first-child{{ color:var(--text-dim); }}
footer{{ margin-top:20px; font-family:var(--mono); font-size:11px; color:var(--text-dim); text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="backlink" href="index.html">\u2190 All Sites</a>
  <header>
    <div>
      <h1>Client Search</h1>
      <div class="subtitle">{len(clients)} clients across {len({c["site"] for c in clients})} site(s) \u00b7 generated {generated_at}</div>
    </div>
    <div class="header-actions">
      <button class="icon-btn" id="theme-toggle" title="Toggle theme">\U0001F319</button>
    </div>
  </header>

  <div class="search-row">
    <input id="search" type="text" placeholder="Search by MAC, IP, site, switch, interface, or VLAN (min {CLIENT_SEARCH_MIN_QUERY_LENGTH} characters)\u2026" autocomplete="off">
  </div>
  <div id="status">Type at least {CLIENT_SEARCH_MIN_QUERY_LENGTH} characters to search.</div>
  <div id="results"></div>

  <footer>Data reflects the last time each site was scanned - it may be out of date. Verify anything found here before relying on it.</footer>
</div>
<script>
const CLIENTS = {clients_json};
const MIN_LEN = {CLIENT_SEARCH_MIN_QUERY_LENGTH};
const MAX_RESULTS = {CLIENT_SEARCH_MAX_RESULTS};

function normalizeMac(s) {{
  return (s || "").replace(/[^0-9a-fA-F]/g, "").toLowerCase();
}}

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}})[c]);
}}

function highlight(text, query) {{
  const t = String(text || "");
  const idx = t.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return escapeHtml(t);
  return escapeHtml(t.slice(0, idx)) + '<span class="match">' + escapeHtml(t.slice(idx, idx + query.length)) + '</span>' + escapeHtml(t.slice(idx + query.length));
}}

function macHtml(client, query) {{
  // Plain substring match gets precise highlighting like every other
  // field. A MAC that only matches once separators/case are stripped
  // (e.g. typing "aabbccdd" against a stored "aa:bb:cc:dd:ee:ff") gets
  // the whole field marked instead - mapping an exact substring range
  // back through a stripped-separator transform isn't worth the
  // complexity for what's already a secondary matching pass.
  if (client.mac.toLowerCase().includes(query.toLowerCase())) {{
    return highlight(client.mac, query);
  }}
  const normQuery = normalizeMac(query);
  if (normQuery.length > 0 && normalizeMac(client.mac).includes(normQuery)) {{
    return '<span class="match">' + escapeHtml(client.mac) + '</span>';
  }}
  return escapeHtml(client.mac);
}}

function matchesClient(client, query) {{
  const q = query.toLowerCase();
  const fields = ["mac", "site", "siteName", "switch", "iface", "vlan", "ip",
                  "model", "serial", "risStatus", "risIp", "phoneNumber"];
  for (const f of fields) {{
    if (String(client[f] || "").toLowerCase().includes(q)) return true;
  }}
  const normQuery = normalizeMac(query);
  if (normQuery.length > 0 && normalizeMac(client.mac).includes(normQuery)) return true;
  return false;
}}

function renderRow(client, query) {{
  const href = "site-" + client.site + ".html";
  let badges = "";
  let typeLabel = client.type === "cisco_phone" ? "phone" : (client.type === "vtc" ? "VTC" : "");
  if (typeLabel) {{
    if (client.model) typeLabel += " \u00b7 " + client.model;
    badges += '<span class="badge">' + escapeHtml(typeLabel) + '</span>';
  }}
  if (client.stale) badges += '<span class="badge stale">stale</span>';

  const hasExpand = client.serial || client.risStatus || client.risIp || client.phoneNumber;
  const panelId = "search-" + client.site + "-" + normalizeMac(client.mac);
  const mainAttrs = hasExpand ? ' onclick="toggleExpand(\\'' + panelId + '\\')" style="cursor:pointer;"' : "";

  let html = '<div class="row">'
    + '<div class="row-main"' + mainAttrs + '>'
    + '<div class="main">'
    + '<span class="name">' + highlight(client.siteName || client.site, query) + '</span>'
    + '<span class="meta">' + highlight(client.switch || "-", query)
    + ' \u00b7 ' + highlight(client.iface || "-", query)
    + ' \u00b7 VLAN ' + highlight(client.vlan || "-", query)
    + ' \u00b7 ' + highlight(client.ip || "-", query)
    + ' \u00b7 ' + macHtml(client, query)
    + '</span></div>'
    + '<div>' + badges + '</div>'
    + '</div>'
    + '<a class="row-link" href="' + href + '" title="Open the ' + escapeHtml(client.siteName || client.site) + ' site page">\u2197</a>'
    + '</div>';

  if (hasExpand) {{
    html += '<div id="' + panelId + '" class="remote-list" hidden>'
      + '<div class="remote-list-title">CUCM / RIS Details</div>'
      + '<div class="enrich-row"><span>Phone number(s)</span><span>' + escapeHtml(client.phoneNumber || "-") + '</span></div>'
      + '<div class="enrich-row"><span>Serial</span><span>' + escapeHtml(client.serial || "-") + '</span></div>'
      + '<div class="enrich-row"><span>RIS status</span><span>' + escapeHtml(client.risStatus || "-") + '</span></div>'
      + '<div class="enrich-row"><span>RIS IP</span><span>' + escapeHtml(client.risIp || "-") + '</span></div>'
      + '</div>';
  }}

  return html;
}}

function toggleExpand(id) {{
  const el = document.getElementById(id);
  el.hidden = !el.hidden;
}}

function runSearch() {{
  const query = document.getElementById("search").value.trim();
  const status = document.getElementById("status");
  const results = document.getElementById("results");

  if (query.length < MIN_LEN) {{
    status.textContent = "Type at least " + MIN_LEN + " characters to search.";
    results.innerHTML = "";
    return;
  }}

  const matched = CLIENTS.filter(c => matchesClient(c, query));
  const shown = matched.slice(0, MAX_RESULTS);

  status.textContent = matched.length > MAX_RESULTS
    ? "Showing " + MAX_RESULTS + " of " + matched.length + " results \u2014 refine your search."
    : matched.length + " result(s).";

  results.innerHTML = shown.map(c => renderRow(c, query)).join("");
}}

document.getElementById("search").addEventListener("input", runSearch);
{THEME_TOGGLE_JS}
</script>
</body>
</html>'''


def generate_client_search_page(conn) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "clients.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_client_search_page(conn))
    return path


def render_exports_page() -> str:
    """Lists whatever CSV files currently exist under OUTPUT_DIR/exports
    - a plain filesystem scan, no database access at all. Deliberately
    NOT called from generate_all()/generate_one() - this page only
    updates when csv_export.py itself calls generate_exports_page(),
    right after writing a file, so it never goes stale relative to
    what's actually in the folder, and never updates on an unrelated
    routine dashboard regeneration either.
    """
    exports_dir = os.path.join(OUTPUT_DIR, "exports")
    files = []
    if os.path.isdir(exports_dir):
        for fname in sorted(os.listdir(exports_dir)):
            if fname.lower().endswith(".csv"):
                fpath = os.path.join(exports_dir, fname)
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fpath), tz=datetime.timezone.utc)
                files.append({"name": fname, "mtime": mtime.strftime("%Y-%m-%d %H:%M UTC")})

    if files:
        rows_html = "".join(f'''
    <a class="row" href="exports/{_esc(f["name"])}" download>
      <div class="main">
        <span class="name">{_esc(f["name"])}</span>
      </div>
      <div class="meta">{_esc(f["mtime"])}</div>
    </a>''' for f in files)
    else:
        rows_html = (
            '<p style="padding:14px; font-family:var(--mono); font-size:12px; color:var(--text-dim);">'
            'No exports yet - run utilities/csv_export.py (or the Export tab in gui.py) to create one.</p>'
        )

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f'''<!DOCTYPE html>
<html data-theme="light">
<head>
<meta charset="UTF-8">
<title>Exports \u2014 Site Awareness Dashboard</title>
<style>
{CSS_TOKENS}
body{{ margin:0; background:var(--bg); color:var(--text); font-family:var(--sans); }}
.wrap{{ max-width:800px; margin:0 auto; padding:24px; }}
a{{ color:inherit; }}
header{{ display:flex; justify-content:space-between; align-items:flex-start; padding-bottom:16px; border-bottom:1px solid var(--border); margin-bottom:20px; }}
header h1{{ margin:0; font-size:22px; font-weight:700; }}
.subtitle{{ font-family:var(--mono); font-size:12px; color:var(--text-dim); margin-top:4px; }}
.backlink{{ display:inline-block; font-family:var(--mono); font-size:12px; color:var(--text-dim); border:1px solid var(--border); border-radius:999px; padding:5px 12px; text-decoration:none; margin-bottom:14px; }}
.backlink:hover{{ color:var(--accent); border-color:var(--accent); }}
button, .btn{{ background:var(--panel-2); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:var(--sans); font-size:13px; padding:8px 14px; cursor:pointer; transition:border-color .15s ease; text-decoration:none; display:inline-block; }}
button:hover, .btn:hover{{ border-color:var(--accent); }}
.header-actions{{ display:flex; gap:8px; align-items:center; }}
.icon-btn{{ width:38px; height:38px; padding:0; display:flex; align-items:center; justify-content:center; font-size:16px; }}
.panel{{ background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }}
.row{{ display:flex; justify-content:space-between; align-items:center; gap:14px; padding:12px 14px; border-bottom:1px solid var(--border); text-decoration:none; color:inherit; }}
.row:last-child{{ border-bottom:none; }}
.row:hover{{ background:var(--panel-2); }}
.row .name{{ font-family:var(--mono); font-size:13px; font-weight:600; }}
.row .meta{{ font-family:var(--mono); font-size:11.5px; color:var(--text-dim); white-space:nowrap; }}
footer{{ margin-top:20px; font-family:var(--mono); font-size:11px; color:var(--text-dim); text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="backlink" href="index.html">\u2190 All Sites</a>
  <header>
    <div>
      <h1>Exports</h1>
      <div class="subtitle">{len(files)} file(s) \u00b7 generated {generated_at}</div>
    </div>
    <div class="header-actions">
      <button class="icon-btn" id="theme-toggle" title="Toggle theme">\U0001F319</button>
    </div>
  </header>

  <div class="panel">{rows_html}</div>

  <footer>Exports only reflect the last time csv_export.py was run - they don't update on every dashboard regeneration.</footer>
</div>
<script>
{THEME_TOGGLE_JS}
</script>
</body>
</html>'''


def generate_exports_page() -> str:
    os.makedirs(os.path.join(OUTPUT_DIR, "exports"), exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "exports.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_exports_page())
    return path


def generate_site(conn, site_row) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"site-{site_row['site_octet']}.html"
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_site_page(conn, site_row))
    return path


def generate_index(conn) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_index_page(conn))
    return path


def generate_all(db_path: str = None):
    db_path = db_path or db.DB_PATH
    with db.get_conn(db_path) as conn:
        sites = db.get_all_sites(conn)
        for site in sites:
            path = generate_site(conn, site)
            print(f"  Wrote {path}")
        index_path = generate_index(conn)
        print(f"  Wrote {index_path}")
        clients_path = generate_client_search_page(conn)
        print(f"  Wrote {clients_path}")
    print(f"Generated {len(sites)} site page(s) + index.")


def generate_one(site_key: str, db_path: str = None):
    db_path = db_path or db.DB_PATH
    with db.get_conn(db_path) as conn:
        site = db.find_site_by_any_key(conn, site_key)
        if site is None:
            print(f"No site found matching '{site_key}'.", file=sys.stderr)
            sys.exit(1)
        path = generate_site(conn, site)
        print(f"  Wrote {path}")
        index_path = generate_index(conn)
        print(f"  Wrote {index_path}")
        clients_path = generate_client_search_page(conn)
        print(f"  Wrote {clients_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", metavar="KEY", help="Regenerate just one site (plus the index) instead of everything")
    args = parser.parse_args()

    if args.site:
        generate_one(args.site)
    else:
        generate_all()
