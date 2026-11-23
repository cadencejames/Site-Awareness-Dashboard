"""
orion_match.py - decides which Orion nodes are SAD devices.

Only devices SAD knows about matter for outage tracking; every other node
Orion monitors (servers, workstations, printers, ...) is ignored. This module
matches Orion nodes to SAD devices and reports what it could not match.

match_nodes() is a pure function (no database, no network). load_sad_devices()
reads SAD's database through db.py's public helpers and does not write to it.

Matching, per Orion node, in this order:
  1. IP address. The node's IP is compared with every IP SAD holds for each
     device (its cached management IP plus everything in device_ips). If exactly
     one device owns it, that is the match ("ip"). If several devices share the
     IP, the node's name breaks the tie ("ip+name"); if it cannot, the node is
     reported as ambiguous and not matched.
  2. Name. If the IP matches nothing, the node's caption, system name and DNS
     name are compared with SAD hostnames (case-insensitive, domain suffix
     ignored). Exactly one device must fit ("hostname"); several is ambiguous.
  3. Otherwise the node is ignored (not a SAD device).
"""
import orion_parser as op


def normalize_host(name):
    """Lower-case host name without its domain suffix. An IP address is kept whole."""
    text = (name or "").strip().lower()
    if not text:
        return ""
    if op.normalize_ip(text):
        return text
    return text.split(".")[0]


def load_sad_devices(conn):
    """Every live SAD device as a plain dict, read through db.py's helpers.

    Skipped: the reserved Unassigned site, deleted sites and devices, and devices
    someone has marked stale (confirmed gone). Each dict has device_id, site_id,
    site_octet, site_name, hostname, ips (set of IPv4 strings) and is_seed."""
    import db
    devices = []
    for site in db.get_all_sites(conn, include_unassigned=False):
        for dev in db.get_devices_for_site(conn, site["id"]):
            if dev["marked_stale_at"]:
                continue
            raw_ips = [dev["mgmt_ip"]] + [row["ip"] for row in db.get_device_ip_rows(conn, dev["id"])]
            ips = {op.normalize_ip(ip) for ip in raw_ips if ip}
            ips.discard(None)
            devices.append({
                "device_id": dev["id"],
                "site_id": site["id"],
                "site_octet": str(site["site_octet"]),
                "site_name": site["site_name"] or "",
                "hostname": dev["hostname"],
                "ips": ips,
                "is_seed": bool(dev["is_seed"]),
            })
    return devices


def match_nodes(nodes, devices):
    """Match normalised Orion nodes to SAD devices.

    Returns a dict:
      matches            {node_id: {"device_id": id, "method": "ip" | "ip+name" | "hostname"}}
      unmatched          node ids that are not SAD devices (ignored)
      ambiguous          [{"node_id", "device_ids", "why"}] - could not be decided, not matched
      node_ids_by_device {device_id: [node ids]} for matched devices
      multi_node_devices device ids that more than one Orion node matched
      devices_without_node  SAD device ids with no Orion node at all
      octet_mismatch     matched node ids whose IP second octet differs from the SAD site's octet
    """
    by_id = {d["device_id"]: d for d in devices}
    by_ip, by_host = {}, {}
    for dev in devices:
        for ip in dev["ips"]:
            by_ip.setdefault(ip, set()).add(dev["device_id"])
        host = normalize_host(dev["hostname"])
        if host:
            by_host.setdefault(host, set()).add(dev["device_id"])

    matches, unmatched, ambiguous = {}, [], []
    for node in sorted(nodes, key=lambda n: n["node_id"]):
        names = {normalize_host(x) for x in (node["caption"], node["sysname"], node["dns"])}
        names.discard("")
        pick, method = None, None
        candidates = by_ip.get(node["ip"], set()) if node["ip"] else set()
        if len(candidates) == 1:
            pick, method = next(iter(candidates)), "ip"
        elif len(candidates) > 1:
            named = {d for d in candidates if normalize_host(by_id[d]["hostname"]) in names}
            if len(named) == 1:
                pick, method = next(iter(named)), "ip+name"
            else:
                ambiguous.append({"node_id": node["node_id"], "device_ids": sorted(candidates),
                                  "why": "IP address shared by several SAD devices"})
                continue
        else:
            named = set()
            for host in names:
                named |= by_host.get(host, set())
            if len(named) == 1:
                pick, method = next(iter(named)), "hostname"
            elif len(named) > 1:
                ambiguous.append({"node_id": node["node_id"], "device_ids": sorted(named),
                                  "why": "name shared by several SAD devices"})
                continue
            else:
                unmatched.append(node["node_id"])
                continue
        matches[node["node_id"]] = {"device_id": pick, "method": method}

    node_ids_by_device = {}
    for node_id, info in matches.items():
        node_ids_by_device.setdefault(info["device_id"], []).append(node_id)
    node_by_id = {n["node_id"]: n for n in nodes}
    octet_mismatch = []
    for node_id, info in matches.items():
        ip = node_by_id[node_id]["ip"]
        if ip and str(op.second_octet(ip)) != by_id[info["device_id"]]["site_octet"]:
            octet_mismatch.append(node_id)
    return {
        "matches": matches,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "node_ids_by_device": node_ids_by_device,
        "multi_node_devices": sorted(d for d, ids in node_ids_by_device.items() if len(ids) > 1),
        "devices_without_node": sorted(d["device_id"] for d in devices if d["device_id"] not in node_ids_by_device),
        "octet_mismatch": sorted(octet_mismatch),
    }


def sites_lacking_seed_node(devices, result):
    """Sites where outage tracking would have no deciding node: the site has no
    seed device flagged, or its seed has no Orion node. Returns a list of
    {"site_octet", "site_name", "reason"}, ordered by site octet."""
    sites = {}
    for dev in devices:
        info = sites.setdefault(dev["site_octet"], {"site_name": dev["site_name"], "has_seed": False, "seed_in_orion": False})
        if dev["is_seed"]:
            info["has_seed"] = True
            if dev["device_id"] in result["node_ids_by_device"]:
                info["seed_in_orion"] = True
    problems = []
    for octet, info in sites.items():
        if not info["has_seed"]:
            problems.append({"site_octet": octet, "site_name": info["site_name"], "reason": "no seed device flagged in SAD"})
        elif not info["seed_in_orion"]:
            problems.append({"site_octet": octet, "site_name": info["site_name"], "reason": "seed device has no Orion node"})
    problems.sort(key=lambda p: (not p["site_octet"].isdigit(), int(p["site_octet"]) if p["site_octet"].isdigit() else 0, p["site_octet"]))
    return problems