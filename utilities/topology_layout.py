"""
topology_layout.py - Pure layout algorithm for a site's topology diagram.
No HTML/SVG here at all - this takes devices/links and returns computed
column/row placements plus consolidated edges, so the layout logic can
be tested and reasoned about independently of how it eventually gets
drawn.

Design (agreed on before writing this):
  - Layered layout: column = hop-distance (BFS) from the site's seed
    device, walked over SAME-SITE links only. Cross-site neighbors are
    never traversed into (matching orchestrator.py's own walk logic) -
    they're always terminal.
  - Within a column, devices are ordered by the last octet of their
    mgmt_ip (ascending) - a more useful reading order for a network
    engineer than alphabetical hostname sort.
  - HA/VIP pairs are detected via a shared, non-null arp_override_ip -
    an unambiguous, already-real signal, not a naming guess. Paired
    devices are kept vertically adjacent in their column.
  - A device collapses into its parent's "N devices" leaf group only
    if ALL of these hold: exactly one same-site neighbor, exactly one
    physical link to that neighbor (a real multi-link pair is always
    kept visible - that redundancy is meaningful, not noise), no
    cross-site edges of its own, and the parent's total count of such
    leaves exceeds LEAF_COLLAPSE_THRESHOLD. A device with any topology
    of its own (more links, or its own cross-site reach) is never
    collapsed, regardless of how many siblings it has.
  - Cross-site fan-outs collapse per (local_device_id, local_intf) -
    matching the earlier VPN/tunnel design decision - even a single
    remote neighbor on that interface still becomes a (size-1) group,
    for a uniform, always-expandable representation rather than
    special-casing the count.
"""

import ipaddress
from collections import defaultdict, deque

LEAF_COLLAPSE_THRESHOLD = 6


def _last_octet_key(ip: str):
    """Sort key: real last-octet value when parseable, otherwise sorts
    after every real IP (so devices with no known IP don't scramble
    the ordering of ones that do).
    """
    if not ip:
        return (1, 0)
    try:
        return (0, int(ipaddress.ip_address(ip)) & 0xFF)
    except ValueError:
        return (1, 0)


def compute_layout(seed_id, devices: dict, links: list, current_site_id,
                    leaf_collapse_threshold: int = LEAF_COLLAPSE_THRESHOLD):
    """
    devices: dict[device_id] -> {"id", "hostname", "mgmt_ip",
        "arp_override_ip", "site_id", "site_octet" (for cross-site
        devices, the owning site's octet/name for display), "site_name"}
        Must include an entry for every device_id referenced by `links`,
        including cross-site remote devices (as stubs - just enough to
        label them).
    links: list of {"device_a_id", "device_b_id", "local_intf",
        "remote_intf"} for this site only (device_a is always local;
        device_b may be local or a cross-site remote device).
    seed_id: device_id to lay the columns out from.
    current_site_id: this site's id, used to tell a local neighbor from
        a cross-site one by comparing to each device's own site_id.

    Returns {"columns": [[unit, ...], ...], "edges": [...]}
    where a "unit" is one of:
        {"kind": "device", "device_id": ...}
        {"kind": "ha_pair", "device_ids": [id, id, ...]}
        {"kind": "leaf_group", "parent_id": ..., "device_ids": [...]}
        {"kind": "cross_site_group", "parent_id": ..., "local_intf": ...,
         "members": [{"hostname":..., "site_octet":..., "site_name":...}, ...]}
    and an edge is:
        {"from": device_id, "to": unit_ref, "kind": "normal"|"cross_site",
         "parallel": N, "local_intf":..., "remote_intf":...}
    unit_ref is a device_id for normal edges, or a synthetic group key
    (see below) for edges into a leaf_group/cross_site_group.
    """
    # --- Step 1: split links into same-site vs cross-site, consolidate
    # parallel (same-pair, multiple physical link) same-site edges. ---
    same_site_pairs = defaultdict(list)  # frozenset({a,b}) -> [ (local_intf, remote_intf), ... ]
    cross_site_by_iface = defaultdict(list)  # (local_device_id, local_intf) -> [(device_b_id, stale), ...]

    for link in links:
        a, b = link["device_a_id"], link["device_b_id"]
        if a == b:
            # A device recorded as linked to itself - not meaningful to
            # draw as topology, and frozenset((a, b)) would collapse to
            # a single element for a == b, breaking the pairing below.
            continue
        b_site = devices[b]["site_id"]
        link_stale = link.get("stale", False)
        if b_site != current_site_id:
            cross_site_by_iface[(a, link["local_intf"])].append((b, link_stale))
        else:
            same_site_pairs[frozenset((a, b))].append({
                "local_intf": link["local_intf"], "remote_intf": link["remote_intf"],
                "orig_a": a, "orig_b": b, "stale": link_stale,
            })

    # --- Step 2: same-site adjacency (for BFS + leaf-eligibility) ---
    same_site_adj = defaultdict(set)
    for pair in same_site_pairs:
        a, b = tuple(pair)
        same_site_adj[a].add(b)
        same_site_adj[b].add(a)

    # --- Step 3: BFS from seed over same-site links only ---
    hop = {seed_id: 0}
    queue = deque([seed_id])
    while queue:
        current = queue.popleft()
        for neighbor in sorted(same_site_adj[current]):
            if neighbor not in hop:
                hop[neighbor] = hop[current] + 1
                queue.append(neighbor)

    # --- Step 4: cross-site edge count per local device (for leaf test) ---
    cross_site_count_by_device = defaultdict(int)
    for (local_device_id, _intf), remotes in cross_site_by_iface.items():
        cross_site_count_by_device[local_device_id] += len(remotes)

    # --- Step 5: leaf eligibility ---
    def is_leaf_eligible(device_id):
        if device_id == seed_id:
            return False
        neighbors = same_site_adj[device_id]
        if len(neighbors) != 1:
            return False
        parent = next(iter(neighbors))
        link_count = len(same_site_pairs[frozenset((device_id, parent))])
        if link_count != 1:
            return False  # a real multi-link pair stays visible, not collapsed
        if cross_site_count_by_device[device_id]:
            return False
        return True

    # Group leaf-eligible devices by their parent, capturing which port
    # on the parent each one plugs into (useful for the expand panel,
    # since the collapsed line itself has no single "the interface" to
    # show a hover tooltip for - many different leaves, many different
    # ports).
    leaves_by_parent = defaultdict(list)
    for device_id in hop:
        if is_leaf_eligible(device_id):
            parent = next(iter(same_site_adj[device_id]))
            variant = same_site_pairs[frozenset((device_id, parent))][0]
            parent_intf = variant["local_intf"] if variant["orig_a"] == parent else variant["remote_intf"]
            leaves_by_parent[parent].append((device_id, parent_intf, variant.get("stale", False)))

    collapsed_leaf_ids = set()
    leaf_groups = {}  # parent_id -> group dict
    for parent, leaf_entries in leaves_by_parent.items():
        if len(leaf_entries) > leaf_collapse_threshold:
            leaf_entries_sorted = sorted(leaf_entries, key=lambda entry: _last_octet_key(devices[entry[0]]["mgmt_ip"]))
            leaf_ids_sorted = [device_id for device_id, _parent_intf, _stale in leaf_entries_sorted]
            leaf_groups[parent] = {
                "kind": "leaf_group", "parent_id": parent, "device_ids": leaf_ids_sorted,
                "leaf_ports": {device_id: parent_intf for device_id, parent_intf, _stale in leaf_entries_sorted},
                "leaf_stale": {device_id: stale for device_id, _parent_intf, stale in leaf_entries_sorted},
            }
            collapsed_leaf_ids.update(leaf_ids_sorted)

    # --- Step 6: HA/VIP pairing via shared arp_override_ip ---
    override_groups = defaultdict(list)
    for device_id in hop:
        override_ip = devices[device_id].get("arp_override_ip")
        if override_ip:
            override_groups[override_ip].append(device_id)
    ha_pair_of = {}  # device_id -> pair unit key (the override_ip)
    for override_ip, member_ids in override_groups.items():
        if len(member_ids) >= 2:
            for device_id in member_ids:
                ha_pair_of[device_id] = override_ip

    # --- Step 7: assemble per-device "unit" placements, column by column ---
    columns = defaultdict(list)  # column_index -> [unit, ...] (unsorted, sorted at the end)
    placed_ha_pairs = set()

    for device_id, column in hop.items():
        if device_id in collapsed_leaf_ids:
            continue
        if device_id in ha_pair_of:
            pair_key = ha_pair_of[device_id]
            if pair_key in placed_ha_pairs:
                continue
            placed_ha_pairs.add(pair_key)
            member_ids = sorted(
                override_groups[pair_key],
                key=lambda d: _last_octet_key(devices[d]["mgmt_ip"]),
            )
            pair_column = min(hop[m] for m in member_ids if m in hop)
            columns[pair_column].append({"kind": "ha_pair", "device_ids": member_ids, "pair_key": pair_key})
        else:
            columns[column].append({"kind": "device", "device_id": device_id})

    # Leaf groups sit one column past their parent.
    for parent, group in leaf_groups.items():
        columns[hop[parent] + 1].append(group)

    # Cross-site groups sit one column past whichever local device owns
    # the interface.
    for (local_device_id, local_intf), remote_entries in cross_site_by_iface.items():
        members = []
        for remote_id, remote_stale in remote_entries:
            d = devices[remote_id]
            members.append({
                "hostname": d["hostname"],
                "site_octet": d.get("site_octet"),
                "site_name": d.get("site_name"),
                "stale": remote_stale,
            })
        columns[hop[local_device_id] + 1].append({
            "kind": "cross_site_group",
            "parent_id": local_device_id,
            "local_intf": local_intf,
            "members": members,
            # The group's own drawn line/box is only marked stale when
            # EVERY remote member is stale - a partially-stale group
            # still has at least one confirmed-active connection, so
            # flagging the whole thing would be misleading. Individual
            # member staleness is still visible above, per-member.
            "stale": all(m["stale"] for m in members) if members else False,
        })

    # --- Step 8: sort each column by last-octet of a representative IP ---
    def unit_sort_key(unit):
        if unit["kind"] == "device":
            return _last_octet_key(devices[unit["device_id"]]["mgmt_ip"])
        if unit["kind"] == "ha_pair":
            return _last_octet_key(devices[unit["device_ids"][0]]["mgmt_ip"])
        if unit["kind"] == "leaf_group":
            return _last_octet_key(devices[unit["device_ids"][0]]["mgmt_ip"])
        if unit["kind"] == "cross_site_group":
            # no local IP concept; sort after devices, by hostname
            return (2, unit["members"][0]["hostname"] if unit["members"] else "")
        raise ValueError(f"unit_sort_key: unrecognized unit kind {unit['kind']!r} - {unit!r}")

    max_column = max(columns) if columns else 0
    ordered_columns = []
    for col_index in range(max_column + 1):
        units = sorted(columns.get(col_index, []), key=unit_sort_key)
        ordered_columns.append(units)

    # --- Step 9: edges ---
    edges = []
    seen_edge_keys = set()
    for pair, link_variants in same_site_pairs.items():
        a, b = link_variants[0]["orig_a"], link_variants[0]["orig_b"]
        # Route an endpoint that got folded into a leaf_group or ha_pair
        # to that unit instead of the raw device id.
        def resolve(node_id):
            if node_id in collapsed_leaf_ids:
                parent = next(iter(same_site_adj[node_id]))
                return ("leaf_group", parent)
            if node_id in ha_pair_of:
                return ("ha_pair", ha_pair_of[node_id])
            return ("device", node_id)

        ra_kind, ra_ref = resolve(a)
        rb_kind, rb_ref = resolve(b)
        if (ra_kind, ra_ref) == (rb_kind, rb_ref):
            # Both real endpoints resolved to the exact same displayed
            # unit - either both collapsed into the same leaf_group, or
            # (the common real case) a genuine direct peer-link/vPC
            # cable between the two devices of one ha_pair, where both
            # ends resolve to that same pair unit. Either way there's
            # nothing meaningful to draw: a line from a box back to
            # itself has no useful visual meaning, and previously
            # rendered as a spurious link-colored line straight through
            # the pair's own gap between its two stacked boxes.
            continue

        # Multiple individually-collapsed leaves under the same parent
        # would otherwise each contribute their own edge into the same
        # leaf_group - dedupe down to one line per unique (from,to) pair.
        # (This never fires for genuine multi-cable pairs between the
        # same two devices - those are already pre-grouped into one
        # same_site_pairs entry, with "parallel" reflecting the real
        # cable count.)
        edge_key = (ra_kind, ra_ref, rb_kind, rb_ref)
        if edge_key in seen_edge_keys:
            continue
        seen_edge_keys.add(edge_key)

        local_intf, remote_intf = link_variants[0]["local_intf"], link_variants[0]["remote_intf"]
        edges.append({
            "from": ra_ref, "from_kind": ra_kind,
            "to": rb_ref, "to_kind": rb_kind,
            "kind": "normal", "parallel": len(link_variants),
            "local_intf": local_intf, "remote_intf": remote_intf,
            # One (local_intf, remote_intf) pair per real physical
            # cable, in the same order the renderer draws the parallel
            # offset lines - so each line's hover tooltip can show its
            # own correct interface pair instead of repeating the
            # first one for all of them. Only meaningful when both ends
            # are real devices/ha_pairs (a leaf_group edge collapses
            # many different leaves' own separate cables into one
            # drawn line, so there's no single correct interface pair
            # to expose here - that detail lives on each leaf in the
            # group's own expand panel instead, via leaf_ports).
            "interfaces": [(v["local_intf"], v["remote_intf"], v.get("stale", False)) for v in link_variants],
        })

    for (local_device_id, local_intf), remote_entries in cross_site_by_iface.items():
        edges.append({
            "from": local_device_id, "from_kind": "device",
            "to": (local_device_id, local_intf), "to_kind": "cross_site_group",
            "kind": "cross_site", "parallel": 1,
            "local_intf": local_intf, "remote_intf": None,
            "remote_count": len(remote_entries),
            # Same "all members stale" rule as the group unit itself
            # (see the columns-construction step above) - a partially
            # stale group still has an active connection, so the drawn
            # line shouldn't say otherwise.
            "stale": all(stale for _id, stale in remote_entries) if remote_entries else False,
        })

    return {"columns": ordered_columns, "edges": edges}
