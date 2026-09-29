"""
site_map_layout.py - Pure layout algorithm for the all-site map: sites
as nodes, inter-site connections (CDP cross-site links and matched GRE
tunnels) as edges. No HTML/SVG here - this takes the site/edge data and
returns computed (x, y) positions, so the layout logic can be tested
and reasoned about independently of how it eventually gets drawn. Same
split as topology_layout.py/topology_svg.py for the per-site diagram.

Design (agreed on before writing this):
  - Force-directed (Fruchterman-Reingold) layout, computed here in pure
    Python rather than handed to a client-side JS library - this
    project runs in an air-gapped environment with no CDN-loaded JS,
    so the whole picture has to already be a static, finished SVG by
    the time it's written to disk, the same as the per-site diagram.
  - Deterministic: seeded with a fixed constant (not real randomness),
    and given enough iterations to settle - a static dashboard that
    visually reshuffles every regeneration for no data reason would be
    actively disorienting, so the same underlying data always produces
    the same picture.
  - Only sites with at least one inter-site edge (CDP or tunnel) take
    part in the force simulation. A site with zero known inter-site
    connections is deliberately NOT thrown into the same physics -
    with no edges pulling on it, it would just drift to an arbitrary
    resting point that looks like it means something (proximity to
    some other site) when it doesn't. Instead, isolated sites are laid
    out separately, in their own fixed grid, positioned below the
    force-directed cluster - visible (never hidden - "no connections"
    is itself real information), but never ambiguous about whether
    their position implies a relationship to anything.
  - Repulsion is computed between every pair of connected-cluster
    nodes regardless of which connected component they're in, so
    genuinely separate clusters (e.g. two site-meshes with no tunnel
    or CDP link between them at all) visually push apart from each
    other, not just from directly-linked neighbors.
"""

import random

# Standard Fruchterman-Reingold tuning - k is the "ideal" distance
# between two connected nodes, derived from the canvas area and node
# count so the layout scales sensibly whether there are 4 connected
# sites or 80.
ITERATIONS = 300
CANVAS_WIDTH = 1600
CANVAS_HEIGHT = 1000
MARGIN = 80

# Fixed, not real randomness - see module docstring on why this has to
# be deterministic across regenerations.
RANDOM_SEED = 20260101

ISOLATED_COLUMNS = 8       # how many isolated-site boxes per row
ISOLATED_ROW_HEIGHT = 70
ISOLATED_COL_WIDTH = 170


def _connected_components(node_ids, edges):
    """Plain union-find - only used to decide initial placement offsets
    per component (spreads separate clusters apart from the very first
    iteration, rather than relying on repulsion alone to slowly tease
    them apart from a fully overlapping start, which converges far
    less reliably within a fixed iteration budget).
    """
    parent = {n: n for n in node_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for edge in edges:
        union(edge["site_a_id"], edge["site_b_id"])

    components = {}
    for n in node_ids:
        components.setdefault(find(n), []).append(n)
    return list(components.values())


def compute_layout(site_ids: list, edges: list) -> dict:
    """
    site_ids: every REAL site's id that has at least one qualifying
        inter-site edge (CDP cross-site or matched tunnel) - callers
        decide the connected/isolated split before calling this;
        everything passed in here takes part in the force simulation.
    edges: list of {"site_a_id", "site_b_id", "kind" ("cdp" or
        "tunnel"), "count"} - one entry per (site pair, kind), already
        deduplicated by the caller (see build_site_map_data()).

    Returns {"positions": {site_id: (x, y)}} in canvas coordinates
    (0,0 top-left), within CANVAS_WIDTH x CANVAS_HEIGHT minus MARGIN on
    every side. Isolated-site placement is NOT this function's job -
    see arrange_isolated() below, which is independent of the force
    simulation entirely.
    """
    if not site_ids:
        return {"positions": {}}

    if len(site_ids) == 1:
        return {"positions": {site_ids[0]: (CANVAS_WIDTH / 2, CANVAS_HEIGHT / 2)}}

    rng = random.Random(RANDOM_SEED)
    area = (CANVAS_WIDTH - 2 * MARGIN) * (CANVAS_HEIGHT - 2 * MARGIN)
    k = (area / len(site_ids)) ** 0.5

    # --- initial placement: one blob per connected component, laid
    # out on a coarse grid of component-centers so separate clusters
    # start apart rather than on top of each other. ---
    components = _connected_components(site_ids, edges)
    comp_cols = max(1, int(len(components) ** 0.5 + 0.999))
    comp_spacing_x = (CANVAS_WIDTH - 2 * MARGIN) / max(1, comp_cols)
    comp_spacing_y = (CANVAS_HEIGHT - 2 * MARGIN) / max(1, (len(components) + comp_cols - 1) // comp_cols)

    pos = {}
    for comp_index, member_ids in enumerate(components):
        comp_row, comp_col = divmod(comp_index, comp_cols)
        center_x = MARGIN + comp_spacing_x * (comp_col + 0.5)
        center_y = MARGIN + comp_spacing_y * (comp_row + 0.5)
        spread = k * max(1, len(member_ids)) ** 0.5
        for site_id in member_ids:
            pos[site_id] = [
                center_x + (rng.random() - 0.5) * spread,
                center_y + (rng.random() - 0.5) * spread,
            ]

    # --- Fruchterman-Reingold main loop ---
    temperature = (CANVAS_WIDTH - 2 * MARGIN) * 0.1
    cooling = temperature / ITERATIONS

    for _iteration in range(ITERATIONS):
        disp = {site_id: [0.0, 0.0] for site_id in site_ids}

        # Repulsion: every pair, regardless of component.
        for i, a in enumerate(site_ids):
            for b in site_ids[i + 1:]:
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                dist = max(0.01, (dx * dx + dy * dy) ** 0.5)
                force = (k * k) / dist
                fx, fy = (dx / dist) * force, (dy / dist) * force
                disp[a][0] += fx
                disp[a][1] += fy
                disp[b][0] -= fx
                disp[b][1] -= fy

        # Attraction: along each edge, scaled by how many real
        # connections it represents (a site pair with 3 CDP links or a
        # matched tunnel plus a CDP link pulls a bit tighter than a
        # single lone connection).
        for edge in edges:
            a, b = edge["site_a_id"], edge["site_b_id"]
            if a not in pos or b not in pos:
                continue
            dx = pos[a][0] - pos[b][0]
            dy = pos[a][1] - pos[b][1]
            dist = max(0.01, (dx * dx + dy * dy) ** 0.5)
            weight = 1.0 + 0.15 * (edge.get("count", 1) - 1)
            force = ((dist * dist) / k) * weight
            fx, fy = (dx / dist) * force, (dy / dist) * force
            disp[a][0] -= fx
            disp[a][1] -= fy
            disp[b][0] += fx
            disp[b][1] += fy

        # Apply displacement, capped by the current temperature, then
        # clamp inside the canvas margins.
        for site_id in site_ids:
            dx, dy = disp[site_id]
            dist = max(0.01, (dx * dx + dy * dy) ** 0.5)
            capped = min(dist, temperature)
            pos[site_id][0] += (dx / dist) * capped
            pos[site_id][1] += (dy / dist) * capped
            pos[site_id][0] = min(CANVAS_WIDTH - MARGIN, max(MARGIN, pos[site_id][0]))
            pos[site_id][1] = min(CANVAS_HEIGHT - MARGIN, max(MARGIN, pos[site_id][1]))

        temperature = max(1.0, temperature - cooling)

    return {"positions": {site_id: (x, y) for site_id, (x, y) in pos.items()}}


def arrange_isolated(isolated_site_ids: list, top_y: float) -> dict:
    """Fixed grid placement for sites with zero qualifying inter-site
    edges, starting at top_y (the caller passes the force-directed
    cluster's own bottom edge, so the isolated row sits cleanly below
    it, never overlapping). Sorted by site_id for a stable, predictable
    order across regenerations - there's no connectivity signal to sort
    by, so this deliberately doesn't try to invent one.

    Returns {"positions": {site_id: (x, y)}, "height": total grid height}.
    """
    positions = {}
    for index, site_id in enumerate(sorted(isolated_site_ids)):
        row, col = divmod(index, ISOLATED_COLUMNS)
        x = MARGIN + col * ISOLATED_COL_WIDTH + ISOLATED_COL_WIDTH / 2
        y = top_y + row * ISOLATED_ROW_HEIGHT + ISOLATED_ROW_HEIGHT / 2
        positions[site_id] = (x, y)
    rows = (len(isolated_site_ids) + ISOLATED_COLUMNS - 1) // ISOLATED_COLUMNS
    height = rows * ISOLATED_ROW_HEIGHT
    return {"positions": positions, "height": height}