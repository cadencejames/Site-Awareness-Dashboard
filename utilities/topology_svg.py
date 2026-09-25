"""
topology_svg.py - Turns a topology_layout.py layout into actual SVG
markup plus the "expand list" HTML panels for leaf/cross-site groups.
Deliberately kept separate from topology_layout.py: that module is
pure position/grouping math with no idea what a pixel is; this one
takes its output and decides where boxes actually sit and what they
look like. Reuses the exact CSS classes already established (and
approved) in site-denver.html - node-box, node-label, node-sub,
link-line, link-line.grouped, grouped-node, remote-list, etc. - so a
generated page looks identical to the hand-built mockup.

Box width is computed dynamically per diagram from the actual longest
label it needs to show, rather than a fixed constant - real hostnames
vary enough in length that a fixed width tuned for short names would
overhang for anything longer. Monospace fonts make this estimate cheap
and accurate: every character is the same width, so required width is
just character-count times a per-font-size factor - no actual
font-metrics measurement needed.
"""

NODE_HEIGHT = 40
ROW_PITCH = 56
HA_GAP = 6          # vertical gap between the two boxes of an HA pair
MARGIN_X = 40
MARGIN_Y = 30
COLUMN_GAP = 90     # horizontal space between one column's box and the next's

MIN_NODE_WIDTH = 110
MAX_NODE_WIDTH = 260
BOX_PADDING = 16    # total horizontal padding inside a box (8px each side)

# Empirical width-per-character-per-font-size-unit for the monospace
# stack this project uses (JetBrains Mono / Consolas / SFMono) - close
# enough across that family that a single ratio works fine here.
MONO_CHAR_WIDTH_RATIO = 0.62
LABEL_FONT_SIZE = 11   # matches .node-label
SUB_FONT_SIZE = 9      # matches .node-sub


def _estimate_text_width(text: str, font_size: int) -> float:
    return len(text or "") * font_size * MONO_CHAR_WIDTH_RATIO


def _truncate_to_width(text: str, font_size: int, max_width: float) -> str:
    """Only used as a last-resort safety net for a label so long it'd
    exceed MAX_NODE_WIDTH even at the widest allowed box - normal
    hostnames should never actually hit this. An SVG <title> child is
    added by the caller so the full value is still available on hover,
    same reasoning as the ARP tables' title-attribute pattern.
    """
    if _estimate_text_width(text, font_size) <= max_width:
        return text
    max_chars = max(1, int(max_width / (font_size * MONO_CHAR_WIDTH_RATIO)) - 1)
    return text[:max_chars] + "\u2026"


def _unit_key(kind: str, ref) -> tuple:
    """Canonical lookup key - matches how topology_layout.py's edges
    reference each kind of unit (see compute_layout()'s docstring).
    """
    return (kind, ref)


def _slot_height(unit: dict) -> int:
    """How many row-slots tall a unit is - an HA pair occupies two
    stacked boxes plus a small gap, everything else is one box.
    """
    return 2 if unit["kind"] == "ha_pair" else 1


def _collect_labels(columns, devices) -> list:
    """Every (text, font_size) pair that will actually be rendered -
    used to compute the one node_width this whole diagram needs.
    """
    labels = []
    for units in columns:
        for unit in units:
            if unit["kind"] == "device":
                d = devices[unit["device_id"]]
                labels.append((d["hostname"], LABEL_FONT_SIZE))
                labels.append((d.get("mgmt_ip") or "", SUB_FONT_SIZE))
            elif unit["kind"] == "ha_pair":
                d1 = devices[unit["device_ids"][0]]
                d2 = devices[unit["device_ids"][1]]
                labels.append((d1["hostname"], LABEL_FONT_SIZE))
                labels.append((d1.get("mgmt_ip") or "", SUB_FONT_SIZE))
                labels.append((d2["hostname"], LABEL_FONT_SIZE))
                labels.append((f"VIP pair w/ {d1['hostname']}", SUB_FONT_SIZE))
            elif unit["kind"] == "leaf_group":
                labels.append((f"{len(unit['device_ids'])} devices", LABEL_FONT_SIZE))
                labels.append(("click to expand", SUB_FONT_SIZE))
            elif unit["kind"] == "cross_site_group":
                count = len(unit["members"])
                labels.append((f"{count} remote site" + ("s" if count != 1 else ""), LABEL_FONT_SIZE))
                labels.append((f"via {unit['local_intf']}", SUB_FONT_SIZE))
            else:
                raise ValueError(f"_collect_labels: unrecognized unit kind {unit['kind']!r} - {unit!r}")
    return labels


def render_svg(layout: dict, devices: dict) -> dict:
    """
    layout: the dict returned by topology_layout.compute_layout()
    devices: same devices dict passed into compute_layout()

    Returns {
        "svg": "<svg ...>...</svg>",
        "width": int, "height": int,
        "expand_panels": [ {"id":, "title":, "items": [{"label":, "sub":}, ...]} ]
    }
    """
    columns = layout["columns"]
    edges = layout["edges"]

    # --- compute this diagram's box width from its actual longest label ---
    labels = _collect_labels(columns, devices)
    required = max(
        (_estimate_text_width(text, size) + BOX_PADDING for text, size in labels),
        default=MIN_NODE_WIDTH,
    )
    node_width = max(MIN_NODE_WIDTH, min(MAX_NODE_WIDTH, required))
    column_pitch = node_width + COLUMN_GAP

    positions = {}   # unit_key -> {"x": box_left_x, "y_top": , "y_center": , "slots": [...] for ha_pair}
    boxes_svg = []
    expand_panels = []

    for col_index, units in enumerate(columns):
        x = MARGIN_X + col_index * column_pitch
        slot = 0
        for unit in units:
            y_top = MARGIN_Y + slot * ROW_PITCH

            if unit["kind"] == "device":
                device = devices[unit["device_id"]]
                y_center = y_top + NODE_HEIGHT / 2
                positions[_unit_key("device", unit["device_id"])] = {
                    "x_left": x, "x_right": x + node_width, "y_center": y_center,
                }
                boxes_svg.append(_render_device_box(x, y_top, device, node_width))

            elif unit["kind"] == "ha_pair":
                y1 = y_top
                y2 = y_top + NODE_HEIGHT + HA_GAP
                y_center = (y1 + y2 + NODE_HEIGHT) / 2
                positions[_unit_key("ha_pair", unit["pair_key"])] = {
                    "x_left": x, "x_right": x + node_width, "y_center": y_center,
                }
                d1 = devices[unit["device_ids"][0]]
                d2 = devices[unit["device_ids"][1]]
                boxes_svg.append(_render_device_box(x, y1, d1, node_width))
                boxes_svg.append(_render_device_box(
                    x, y2, d2, node_width, sub_override=f"VIP pair w/ {d1['hostname']}",
                ))
                boxes_svg.append(_render_ha_bracket(x + node_width, y1, y2))

            elif unit["kind"] == "leaf_group":
                y_center = y_top + NODE_HEIGHT / 2
                key = _unit_key("leaf_group", unit["parent_id"])
                positions[key] = {"x_left": x, "x_right": x + node_width, "y_center": y_center}
                panel_id = f"expand-leaf-{unit['parent_id']}"
                count = len(unit["device_ids"])
                boxes_svg.append(_render_stacked_box(
                    x, y_top, f"{count} devices", "click to expand", panel_id, node_width,
                ))
                expand_panels.append({
                    "id": panel_id,
                    "title": f"{count} devices via {devices[unit['parent_id']]['hostname']}",
                    "items": [
                        {
                            "label": devices[d]["hostname"],
                            "sub": (
                                f"{devices[d].get('mgmt_ip') or ''} \u00b7 {unit['leaf_ports'][d]}"
                                f"{' \u00b7 stale' if unit['leaf_stale'].get(d) else ''}"
                            ).strip(" \u00b7"),
                        }
                        for d in unit["device_ids"]
                    ],
                })

            elif unit["kind"] == "cross_site_group":
                y_center = y_top + NODE_HEIGHT / 2
                key = _unit_key("cross_site_group", (unit["parent_id"], unit["local_intf"]))
                positions[key] = {"x_left": x, "x_right": x + node_width, "y_center": y_center}
                panel_id = f"expand-remote-{unit['parent_id']}-{unit['local_intf']}".replace("/", "_")
                count = len(unit["members"])
                label = f"{count} remote site" + ("s" if count != 1 else "")
                boxes_svg.append(_render_stacked_box(
                    x, y_top, label, f"via {unit['local_intf']}", panel_id, node_width,
                    grouped=True, stale=unit.get("stale", False),
                ))
                expand_panels.append({
                    "id": panel_id,
                    "title": f"{label} reachable via {devices[unit['parent_id']]['hostname']} ({unit['local_intf']})",
                    "items": [
                        {
                            "label": m["hostname"],
                            "sub": (
                                f"{m.get('site_name') or ''} ({m.get('site_octet') or '?'})"
                                f"{' \u00b7 stale' if m.get('stale') else ''}"
                            ),
                        }
                        for m in unit["members"]
                    ],
                })

            else:
                raise ValueError(f"render_svg: unrecognized unit kind {unit['kind']!r} - {unit!r}")

            slot += _slot_height(unit)

    # --- edges, drawn after all positions are known ---
    lines_svg = []
    for edge in edges:
        from_pos = positions.get(_unit_key(edge["from_kind"], edge["from"]))
        to_pos = positions.get(_unit_key(edge["to_kind"], edge["to"]))
        if from_pos is None or to_pos is None:
            continue  # defensive - shouldn't happen if layout and render stay in sync
        is_grouped = edge["kind"] == "cross_site"

        tooltips = None
        line_stale_flags = None
        if edge["kind"] == "cross_site":
            count = edge.get("remote_count", 1)
            suffix = "s" if count != 1 else ""
            tooltips = [f"{edge['local_intf']} \u2192 {count} remote site{suffix}"]
            line_stale_flags = [edge.get("stale", False)]
        elif edge["from_kind"] != "leaf_group" and edge["to_kind"] != "leaf_group":
            # A leaf_group edge collapses many different leaves' own
            # separate cables into one drawn line - there's no single
            # correct interface pair to show here (that detail is on
            # each leaf's own row in the group's expand panel instead).
            interfaces = edge.get("interfaces", [])
            tooltips = [f"{local} \u2192 {remote}" for local, remote, _stale in interfaces]
            line_stale_flags = [stale for _local, _remote, stale in interfaces]

        lines_svg.append(_render_edge(
            from_pos, to_pos, edge["parallel"], grouped=is_grouped, tooltips=tooltips, stale_flags=line_stale_flags,
        ))

    max_col = len(columns)
    max_slots = max((sum(_slot_height(u) for u in col) for col in columns), default=1)
    width = MARGIN_X * 2 + max(max_col, 1) * column_pitch
    height = MARGIN_Y * 2 + max(max_slots, 1) * ROW_PITCH

    svg = (
        f'<svg id="topology" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        + "".join(lines_svg) + "".join(boxes_svg) + "</svg>"
    )
    return {"svg": svg, "width": width, "height": height, "expand_panels": expand_panels}


def _esc(s) -> str:
    return "" if s is None else str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _label_svg(text, cx, y, font_size, css_class, max_width) -> str:
    """A text element that truncates (with an SVG <title> hover
    tooltip carrying the full value) only in the rare case a label
    exceeds even MAX_NODE_WIDTH - normal hostnames/IPs should always
    fit untruncated given the dynamic box sizing above.
    """
    display = _truncate_to_width(text or "", font_size, max_width)
    title = f'<title>{_esc(text)}</title>' if display != (text or "") else ""
    return f'<text class="{css_class}" x="{cx}" y="{y}" text-anchor="middle">{_esc(display)}{title}</text>'


def _render_device_box(x, y, device, node_width, sub_override=None) -> str:
    cx = x + node_width / 2
    inner_width = node_width - BOX_PADDING
    sub_text = sub_override if sub_override is not None else (device.get("mgmt_ip") or "")
    is_stale = device.get("stale")
    if is_stale:
        sub_text = f"stale \u00b7 {sub_text}" if sub_text else "stale"
    box_class = "node-box stale" if is_stale else "node-box"
    return (
        f'<rect class="{box_class}" x="{x}" y="{y}" width="{node_width}" height="{NODE_HEIGHT}" rx="6"/>'
        + _label_svg(device["hostname"], cx, y + 16, LABEL_FONT_SIZE, "node-label", inner_width)
        + _label_svg(sub_text, cx, y + 30, SUB_FONT_SIZE, "node-sub", inner_width)
    )


def _render_stacked_box(x, y, label, sub, panel_id, node_width, grouped=False, stale=False) -> str:
    """A leaf_group or cross_site_group node - a small offset 'shadow'
    rect behind the main one hints visually at 'multiple items' without
    needing any actual shadow/gradient effect (flat rects only, per the
    design system's own rules). cross_site_group additionally uses the
    existing dashed 'grouped' styling; leaf_group stays solid, since
    it's still local/same-site data, not a network boundary. stale is
    only ever passed for cross_site_group (an "all members stale"
    aggregate) - leaf_group's collapsed box deliberately never gets
    this treatment, since many different leaves fold into one box and
    there's no single honest staleness to represent there.
    """
    cx = x + node_width / 2
    inner_width = node_width - BOX_PADDING
    classes = ["node-box"]
    if grouped:
        classes.append("grouped")
    if stale:
        classes.append("stale")
    box_class = " ".join(classes)
    stack = (
        f'<rect class="node-box" x="{x+5}" y="{y+5}" width="{node_width}" height="{NODE_HEIGHT}" rx="6" opacity="0.4"/>'
        if not grouped else ""
    )
    return (
        f'<g class="grouped-node" onclick="toggleExpand(\'{panel_id}\')">'
        f'{stack}'
        f'<rect class="{box_class}" x="{x}" y="{y}" width="{node_width}" height="{NODE_HEIGHT}" rx="6"/>'
        + _label_svg(label, cx, y + 16, LABEL_FONT_SIZE, "node-label", inner_width)
        + _label_svg(sub, cx, y + 30, SUB_FONT_SIZE, "node-sub", inner_width)
        + f'</g>'
    )


def _render_ha_bracket(x_right, y1, y2) -> str:
    top = y1 + NODE_HEIGHT / 2
    bottom = y2 + NODE_HEIGHT / 2
    mid = (top + bottom) / 2
    return (
        f'<path d="M {x_right+5} {top} C {x_right+25} {top}, {x_right+25} {bottom}, {x_right+5} {bottom}" '
        f'fill="none" stroke="var(--border)" stroke-width="1"/>'
        f'<text class="node-sub" x="{x_right+45}" y="{mid+3}" text-anchor="middle">HSRP</text>'
    )


def _line_group(cls, x1, y1, x2, y2, tooltip, hit_width=10) -> str:
    """A thin visible line paired with an invisible, much wider line at
    the same coordinates that exists purely to catch hover events - a
    bare 1.3px stroke (this diagram's link-line width) is too small a
    target for a browser to hit-test precisely, especially with
    several parallel lines just a few pixels apart. Both are wrapped
    in one <g> so a plain CSS :hover rule on the group can highlight
    the thin visible line - no JS needed - and so the tooltip (as a
    child of the group, not either individual line) applies whichever
    of the two you actually land on. pointer-events="stroke" is
    required on the hit line - a transparent stroke isn't hoverable by
    default. hit_width is a parameter (not a fixed constant) because
    for parallel lines it must stay narrower than the actual spacing
    between them - otherwise adjacent hit-targets would overlap and
    reintroduce the same ambiguity this is meant to fix, just via a
    different mechanism.
    """
    title = f'<title>{_esc(tooltip)}</title>' if tooltip else ""
    visible = f'<line class="{cls}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>'
    hit_target = (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
        f'stroke="transparent" stroke-width="{hit_width}" pointer-events="stroke"/>'
    )
    return f'<g class="edge">{title}{visible}{hit_target}</g>'


def _line_class(grouped: bool, stale: bool) -> str:
    classes = ["link-line"]
    if grouped:
        classes.append("grouped")
    if stale:
        classes.append("stale")
    return " ".join(classes)


def _render_edge(from_pos, to_pos, parallel, grouped=False, tooltips=None, stale_flags=None) -> str:
    x1, x2 = from_pos["x_right"], to_pos["x_left"]
    y1, y2 = from_pos["y_center"], to_pos["y_center"]
    tooltips = tooltips or []
    stale_flags = stale_flags or []
    if parallel <= 1:
        tooltip = tooltips[0] if tooltips else None
        stale = stale_flags[0] if stale_flags else False
        cls = _line_class(grouped, stale)
        return _line_group(cls, x1, y1, x2, y2, tooltip, hit_width=10)
    spread = 16  # total vertical spread across all parallel lines for this pair
    spacing = spread / (parallel - 1)
    # Keep each line's wide hit-target narrower than its actual spacing
    # from its neighbors, so adjacent hit-targets never overlap - that
    # would just reintroduce the same hover-precision ambiguity this
    # whole thing exists to fix, via a different mechanism.
    hit_width = min(10, spacing * 0.8)
    lines = []
    offsets = [(-spread / 2 + i * spacing) for i in range(parallel)]
    for i, off in enumerate(offsets):
        # Each physical cable's own hover tooltip and staleness, in the
        # same order the offset lines are drawn - one real cable could
        # be freshly seen while another (in the same multi-link pair)
        # has aged out, so this stays independent per line rather than
        # one flag for the whole edge.
        tooltip = tooltips[i] if i < len(tooltips) else None
        stale = stale_flags[i] if i < len(stale_flags) else False
        cls = _line_class(grouped, stale)
        lines.append(_line_group(cls, x1, y1 + off, x2, y2 + off, tooltip, hit_width=hit_width))
    return "".join(lines)
