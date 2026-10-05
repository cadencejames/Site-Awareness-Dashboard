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

import math
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

# Two nodes settling on top of (or very near) each other is always
# possible in a force-directed layout - a pair with a heavy edge
# weight and few other nodes pulling on it, a small `k` on a large
# fleet, or just an unlucky equilibrium - the physics doesn't promise
# a minimum spacing on its own, only that forces balance out
# *somewhere*. Rather than chase every possible cause, a fixed
# separation floor is enforced as a final pass below: guaranteed
# readable node spacing regardless of what produced the layout.
# Comfortably bigger than twice the largest node radius
# (site_map_svg.MAX_RADIUS = 28) plus room for the label text under
# each node - not derived from that constant directly, to keep this
# module's only concern being positions, not how a site is drawn.
MIN_NODE_SEPARATION = 85
SEPARATION_PASSES = 40

# How much extra repulsion a node's total edge weight buys it against
# every other node - see compute_layout()'s own comment on `weight`
# for why this exists. 0 would be plain unweighted Fruchterman-
# Reingold; tuned empirically so two heavily-connected hubs visibly
# separate without ordinary leaf-leaf pairs being pushed apart much at
# all (their weights are small, so this term stays close to 1.0 for
# them).
HUB_REPULSION_STRENGTH = 0.35

# Gentle Y-axis-only pull between well-connected node pairs - see the
# big comment where this is used, in the main loop, for the reasoning.
# This compounds over ITERATIONS=300 passes, so it climbs to a
# dead-straight line MUCH faster than the number itself suggests - in
# testing against a 3-hub/18-leaf layout, measuring how tightly the
# hubs' Y-coordinates cluster together (0 = no pull at all):
#   0.0005 -> still loose, barely different from no pull
#   0.001   -> noticeably closer, clearly not coincidence, still varied
#   0.002   -> tight grouping, starting to look deliberate
#   0.004+  -> essentially a straight line
# Set conservatively in that "noticeably closer, still varied" range.
# Raise it in small steps (0.0005 at a time) and look at the actual
# rendered map each time - it moves faster than it looks like it
# should.
HUB_ALIGNMENT_STRENGTH = 0.001


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

    # A node's "weight" is the total edge count touching it (e.g. a
    # hub with matched tunnels to five other sites has a higher weight
    # than a site with just one). Plain Fruchterman-Reingold gives
    # every pair of nodes the same repulsion regardless of how
    # connected either one is - so two hub sites, each pulled toward
    # the middle of the layout by many attraction forces, end up right
    # next to each other with nothing pushing back harder than it
    # would for two ordinary leaf sites. HUB_REPULSION_STRENGTH scales
    # repulsion up for pairs where at least one side is well-connected,
    # so hubs actively carve out extra space from each other (and from
    # everything else) instead of just landing on top of each other by
    # coincidence. A random-seed change can accidentally dodge that for
    # one particular dataset, but isn't a real fix - this is.
    weight = {site_id: 0.0 for site_id in site_ids}
    for edge in edges:
        a, b = edge["site_a_id"], edge["site_b_id"]
        w = edge.get("count", 1)
        if a in weight:
            weight[a] += w
        if b in weight:
            weight[b] += w

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

        # Repulsion: every pair, regardless of component. Scaled up for
        # well-connected nodes (see `weight`/HUB_REPULSION_STRENGTH
        # above) so two hubs carve out extra distance from each other,
        # not just from the graph as a whole.
        for i, a in enumerate(site_ids):
            for b in site_ids[i + 1:]:
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                dist = max(0.01, (dx * dx + dy * dy) ** 0.5)
                hub_factor = 1.0 + HUB_REPULSION_STRENGTH * (weight[a] + weight[b])
                force = (k * k * hub_factor) / dist
                fx, fy = (dx / dist) * force, (dy / dist) * force
                disp[a][0] += fx
                disp[a][1] += fy
                disp[b][0] -= fx
                disp[b][1] -= fy

        # Hub alignment: a gentle pull-together on the Y axis only,
        # between every pair of nodes, scaled by BOTH nodes' weight -
        # so it's negligible for two ordinary leaves (small weight on
        # both sides), noticeable between two hubs (large weight on
        # both sides), and in between for a hub/leaf pair. This is
        # deliberately soft rather than a hard "put every hub on one
        # exact line" constraint - a rigid line would fight the
        # organic, settled-into-place look of the rest of the map, and
        # would need its own separate logic to keep hubs from
        # overlapping each other along that line. A gentle bias lets
        # hubs drift toward roughly the same height over the course of
        # the simulation while everything else about their position
        # (X spacing, distance from their own leaves) is still decided
        # by the normal forces above. HUB_ALIGNMENT_STRENGTH is the
        # knob - 0 turns this off entirely.
        if HUB_ALIGNMENT_STRENGTH:
            for i, a in enumerate(site_ids):
                for b in site_ids[i + 1:]:
                    pull = HUB_ALIGNMENT_STRENGTH * weight[a] * weight[b]
                    if pull == 0:
                        continue
                    dy = pos[b][1] - pos[a][1]
                    disp[a][1] += dy * pull
                    disp[b][1] -= dy * pull

        # Attraction: along each edge, scaled by how many real
        # connections it represents (a site pair with 3 matched
        # tunnels pulls a bit tighter than a single lone one).
        for edge in edges:
            a, b = edge["site_a_id"], edge["site_b_id"]
            if a not in pos or b not in pos:
                continue
            dx = pos[a][0] - pos[b][0]
            dy = pos[a][1] - pos[b][1]
            dist = max(0.01, (dx * dx + dy * dy) ** 0.5)
            edge_weight = 1.0 + 0.15 * (edge.get("count", 1) - 1)
            force = ((dist * dist) / k) * edge_weight
            fx, fy = (dx / dist) * force, (dy / dist) * force
            disp[a][0] -= fx
            disp[a][1] -= fy
            disp[b][0] += fx
            disp[b][1] += fy

        # Apply displacement, capped by the current temperature. No
        # per-axis clamping to the canvas edges here on purpose - see
        # the big comment above the main loop for why (it's what used
        # to produce the "rectangle border" look). A generous safety
        # bound (several canvas-widths out) still applies, purely to
        # stop numerical blow-up on pathological input; it should
        # never actually be reached in practice.
        safety_bound = max(CANVAS_WIDTH, CANVAS_HEIGHT) * 4
        for site_id in site_ids:
            dx, dy = disp[site_id]
            dist = max(0.01, (dx * dx + dy * dy) ** 0.5)
            capped = min(dist, temperature)
            pos[site_id][0] += (dx / dist) * capped
            pos[site_id][1] += (dy / dist) * capped
            pos[site_id][0] = min(safety_bound, max(-safety_bound, pos[site_id][0]))
            pos[site_id][1] = min(safety_bound, max(-safety_bound, pos[site_id][1]))

        temperature = max(1.0, temperature - cooling)

    _fit_to_canvas(pos, site_ids)
    _enforce_min_separation(pos, site_ids, rng)

    return {"positions": {site_id: (x, y) for site_id, (x, y) in pos.items()}}


def _fit_to_canvas(pos: dict, site_ids: list) -> None:
    """Rescales and re-centers the whole settled layout to fill the
    canvas (within MARGIN on every side), preserving its actual shape
    and aspect ratio rather than clamping each axis independently.

    This runs once, after the force simulation has fully settled, in
    place of clamping node-by-node on every iteration - clamping
    mid-simulation is what used to flatten any node pushed toward an
    edge onto a straight line at that edge, so several outward nodes
    together read as a crisp rectangle instead of the graph's actual
    (organic, irregular) outer boundary. Letting the physics run
    unconstrained and only fitting the *result* into the canvas
    afterward keeps whatever shape the graph actually settled into.
    Mutates `pos` in place.
    """
    if not site_ids:
        return
    xs = [pos[s][0] for s in site_ids]
    ys = [pos[s][1] for s in site_ids]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(1.0, max_x - min_x)
    span_y = max(1.0, max_y - min_y)

    avail_w = CANVAS_WIDTH - 2 * MARGIN
    avail_h = CANVAS_HEIGHT - 2 * MARGIN
    # Single uniform scale (not independent x/y scales) so the shape
    # isn't stretched out of proportion - whichever axis is tighter
    # relative to its available space decides the scale, same idea as
    # "letterboxing" an image into a frame.
    scale = min(avail_w / span_x, avail_h / span_y)

    # Center the scaled shape within the canvas, rather than pinning
    # it to the MARGIN corner - keeps it visually balanced regardless
    # of the graph's actual aspect ratio.
    scaled_w = span_x * scale
    scaled_h = span_y * scale
    offset_x = MARGIN + (avail_w - scaled_w) / 2.0
    offset_y = MARGIN + (avail_h - scaled_h) / 2.0

    for site_id in site_ids:
        pos[site_id][0] = offset_x + (pos[site_id][0] - min_x) * scale
        pos[site_id][1] = offset_y + (pos[site_id][1] - min_y) * scale


def _enforce_min_separation(pos: dict, site_ids: list, rng: random.Random) -> None:
    """Final pass after the force simulation has settled: pushes apart
    any pair of nodes still closer than MIN_NODE_SEPARATION, so the
    rendered map never shows two site nodes overlapping or crowded
    into unreadable proximity, no matter what caused it (see
    MIN_NODE_SEPARATION's own comment). Mutates `pos` in place.

    Deliberately a separate, simple pass rather than folded into the
    main loop's repulsion force - the main loop is tuned to produce a
    good overall *shape* (clusters near their neighbors, separate
    components apart), and cranking repulsion up further to guarantee
    a hard minimum there would fight that tuning. This only steps in
    for the specific pairs that need it, once the general shape is
    already decided.

    Runs a fixed number of passes (rather than looping until clean)
    since resolving one pair can nudge another pair back under the
    threshold - a few passes converge in practice for the number of
    site nodes this map ever has, and a fixed budget keeps this
    provably bounded rather than a potential infinite loop on some
    pathological input.
    """
    for _pass in range(SEPARATION_PASSES):
        moved = False
        for i, a in enumerate(site_ids):
            for b in site_ids[i + 1:]:
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                dist = (dx * dx + dy * dy) ** 0.5
                if dist >= MIN_NODE_SEPARATION:
                    continue
                moved = True
                if dist < 0.01:
                    # Exactly (or almost exactly) coincident - no real
                    # direction to push along, so pick one at random
                    # rather than leaving them stacked.
                    angle = rng.random() * 6.283185307179586
                    dx, dy = math.cos(angle), math.sin(angle)
                    dist = 1.0
                push = (MIN_NODE_SEPARATION - dist) / 2.0
                ux, uy = dx / dist, dy / dist
                pos[a][0] += ux * push
                pos[a][1] += uy * push
                pos[b][0] -= ux * push
                pos[b][1] -= uy * push
                pos[a][0] = min(CANVAS_WIDTH - MARGIN, max(MARGIN, pos[a][0]))
                pos[a][1] = min(CANVAS_HEIGHT - MARGIN, max(MARGIN, pos[a][1]))
                pos[b][0] = min(CANVAS_WIDTH - MARGIN, max(MARGIN, pos[b][0]))
                pos[b][1] = min(CANVAS_HEIGHT - MARGIN, max(MARGIN, pos[b][1]))
        if not moved:
            break


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