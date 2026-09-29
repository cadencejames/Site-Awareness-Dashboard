"""
site_map_svg.py - Turns a site_map_layout.py layout into actual SVG
markup for the all-site map. Deliberately separate from
site_map_layout.py the same way topology_svg.py is separate from
topology_layout.py: that module is pure position math with no idea
what a pixel is, this one decides what things look like.

Each site is drawn as a circle (radius scaled gently by device count,
not a fixed size - a 40-device hub site and a 2-device closet
shouldn't look identical) with its name/octet labeled below it, and a
small colored ring/status matching the same up/stale convention used
everywhere else in the dashboard (index page's site-list dot,
per-site device staleness). Every node is a real link to that site's
own page - this map is a way IN to the existing per-site pages, not a
replacement for them.

Two distinct edge stylings, matching the per-site diagram's existing
"grouped/dashed = a network boundary, not a plain local cable"
convention:
  - CDP-derived inter-site links: solid, neutral color.
  - Matched tunnel links: dashed, accent color - visually distinct
    since these are the connections that are otherwise invisible
    without this exact feature (a site reachable only over a GRE
    tunnel, not by any direct CDP-discoverable path).
A site with one or more one-sided (unmatched) tunnels gets a small
marker on its own node rather than a dangling edge - there's no known
far end to draw a line to, so surfacing it as "this site has an
unconfirmed tunnel" on the node itself is the honest representation
(see match_tunnels()'s own docstring for why one-sided tunnels are
surfaced instead of hidden in the first place).
"""

MIN_RADIUS = 10
MAX_RADIUS = 28
RADIUS_PER_DEVICE = 0.35

LABEL_FONT_SIZE = 10.5
SUB_FONT_SIZE = 9


def _esc(s) -> str:
    return "" if s is None else str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _radius_for(device_count: int) -> float:
    return min(MAX_RADIUS, max(MIN_RADIUS, MIN_RADIUS + device_count * RADIUS_PER_DEVICE))


def render_svg(layout_positions: dict, isolated_positions: dict, edges: list, site_meta: dict,
                canvas_width: int, canvas_height: int) -> str:
    """
    layout_positions: {site_id: (x, y)} from site_map_layout.compute_layout()
    isolated_positions: {site_id: (x, y)} from site_map_layout.arrange_isolated()
    edges: [{"site_a_id", "site_b_id", "kind", "count"}, ...] - only
        edges between two sites both present in layout_positions are
        drawn (isolated sites have none by definition).
    site_meta: {site_id: {"label", "octet", "href", "device_count",
        "status" ("up"|"stale"|"none"), "one_sided_tunnels"}}
    canvas_width/canvas_height: the full drawing area - isolated_positions
        already accounts for sitting below the force-directed cluster,
        so this is just however tall the caller decided the whole
        thing needs to be.

    Returns the raw '<svg ...>...</svg>' markup.
    """
    all_positions = {**layout_positions, **isolated_positions}

    lines_svg = []
    for edge in edges:
        a, b = edge["site_a_id"], edge["site_b_id"]
        if a not in all_positions or b not in all_positions:
            continue
        x1, y1 = all_positions[a]
        x2, y2 = all_positions[b]
        cls = "site-edge tunnel" if edge["kind"] == "tunnel" else "site-edge"
        count = edge.get("count", 1)
        kind_label = "tunnel" if edge["kind"] == "tunnel" else "CDP"
        tooltip = (
            f'{_esc(site_meta[a]["label"])} ↔ {_esc(site_meta[b]["label"])} '
            f'({count} {kind_label} link{"s" if count != 1 else ""})'
        )
        if edge["kind"] == "tunnel" and edge.get("tunnel_names"):
            # Interface names (e.g. "Tu100") for the matched tunnel(s)
            # behind this edge - otherwise a tunnel link looks the same
            # as any other and you'd have to go find it on the site
            # page to know which tunnel it actually is.
            tooltip += " · " + ", ".join(_esc(name) for name in edge["tunnel_names"])
        width = min(4.5, 1.3 + 0.3 * (count - 1))
        lines_svg.append(
            f'<g class="site-edge-group"><title>{tooltip}</title>'
            f'<line class="{cls}" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke-width="{width:.2f}"/>'
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="transparent" stroke-width="10" pointer-events="stroke"/></g>'
        )

    nodes_svg = []
    for site_id, (x, y) in all_positions.items():
        meta = site_meta[site_id]
        r = _radius_for(meta["device_count"])
        status_cls = f"site-node-{meta['status']}" if meta["status"] != "none" else "site-node-none"
        warn_marker = ""
        if meta.get("one_sided_tunnels"):
            n = meta["one_sided_tunnels"]
            warn_marker = (
                f'<circle class="site-node-warn-dot" cx="{x + r * 0.72:.1f}" cy="{y - r * 0.72:.1f}" r="5">'
                f'<title>{n} unconfirmed tunnel{"s" if n != 1 else ""} (no matching far end yet)</title></circle>'
            )
        nodes_svg.append(
            f'<a href="{_esc(meta["href"])}" class="site-node-link">'
            f'<g class="site-node">'
            f'<circle class="site-node-circle {status_cls}" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}">'
            f'<title>{_esc(meta["label"])} ({_esc(meta["octet"])}) · {meta["device_count"]} device(s)</title>'
            f'</circle>'
            f'{warn_marker}'
            f'<text class="site-node-label" x="{x:.1f}" y="{y + r + 13:.1f}" text-anchor="middle">{_esc(meta["label"])}</text>'
            f'<text class="site-node-sub" x="{x:.1f}" y="{y + r + 24:.1f}" text-anchor="middle">{_esc(meta["octet"])}</text>'
            f'</g></a>'
        )

    return (
        f'<svg id="site-map" viewBox="0 0 {canvas_width} {canvas_height}" xmlns="http://www.w3.org/2000/svg">'
        + "".join(lines_svg) + "".join(nodes_svg) + "</svg>"
    )