"""
outage_pages.py - the outage pages of the static dashboard.

  render_site_panel(conn, site_row)   the "Outages" panel on a site page ("" when there is no outage data)
  render_index_bits(conn)             (button, tile) for the index page, or ("", "")
  generate_outage_pages(conn)         writes dashboard/outages-YYYY-MM.html for every stored month,
                                      dashboard/outages.html (the newest month) and the monthly Excel files
  summary(conn)                       the figures the index tile shows, or None

Every number comes from outage_report.compute_month, the same code the Outages tab,
the command line and the Excel export use. Only stored data is read; nothing here talks to Orion.

Colors follow the rest of the dashboard: good = --up (green), warn = --accent (amber),
bad = --warn (red). The cut-offs live in settings.json:

    "outage": {
        "good_at": 99.9,                  availability at or above this is green
        "warn_at": 99.0,                  at or above this (but below good_at) is amber; below it is red
        "tier_thresholds": {              optional, per tier; a tier left out uses the two values above
            "0": {"good_at": 99.95, "warn_at": 99.5}
        }
    }

Monthly Excel files are written to dashboard/outage_reports/. A month still in progress is
rewritten on every run; once the month is over its file is written one last time and then
left alone, so an exported month stays a fixed snapshot.
"""
import datetime
import html
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "utilities"))

import db  # noqa: E402
import orion_parser as op  # noqa: E402
import outage_calc  # noqa: E402
import outage_db  # noqa: E402
import outage_report as rpt  # noqa: E402
import settings_loader  # noqa: E402
import xlsx_writer  # noqa: E402
import banner  # noqa: E402

OUTPUT_DIR = os.path.join(HERE, "dashboard")
REPORT_DIR = os.path.join(OUTPUT_DIR, "outage_reports")
MAX_MONTHS = 24
DEFAULT_GOOD = 99.9
DEFAULT_WARN = 99.0
CACHE_SECONDS = 60
TIER_NAMES = {0: "Tier 0", 1: "Tier 1", 2: "Tier 2", 3: "Tier 3", 4: "Tier 4"}
TIER_NOTES = {0: "Most important", 3: "Least important", 4: "Offline often because of their own operations"}
LEVEL_CLASS = {"good": "ou-good", "warn": "ou-warn", "bad": "ou-bad", "none": "ou-none"}


def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------------------
# thresholds
# ---------------------------------------------------------------------------

def _threshold(value, default, name):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise rpt.ReportError(f"outage.{name} must be a number such as 99.9, not {value!r}")
    if not 0 <= value <= 100:
        raise rpt.ReportError(f"outage.{name} must be between 0 and 100, not {value!r}")
    return float(value)


def parse_thresholds(section):
    """Validate good_at / warn_at / tier_thresholds. Returns {"good", "warn", "tiers": {tier: (good, warn)}}.
    Raises ReportError with a plain reason."""
    section = section or {}
    good = _threshold(section.get("good_at"), DEFAULT_GOOD, "good_at")
    warn = _threshold(section.get("warn_at"), DEFAULT_WARN, "warn_at")
    if warn > good:
        raise rpt.ReportError("outage.warn_at must not be higher than outage.good_at")
    over = section.get("tier_thresholds")
    if over is None:
        over = {}
    if not isinstance(over, dict):
        raise rpt.ReportError("outage.tier_thresholds must be an object such as {\"0\": {\"good_at\": 99.95}}")
    tiers = {}
    for key, value in over.items():
        try:
            tier = int(str(key).strip().lower().replace("tier", "").strip())
        except ValueError:
            raise rpt.ReportError(f"outage.tier_thresholds has an unknown tier: {key!r}") from None
        if tier not in outage_db.TIERS:
            raise rpt.ReportError(f"outage.tier_thresholds has an unknown tier: {key!r}")
        if not isinstance(value, dict):
            raise rpt.ReportError(f"outage.tier_thresholds.{key} must be an object with good_at and/or warn_at")
        t_good = _threshold(value.get("good_at"), good, f"tier_thresholds.{key}.good_at")
        t_warn = _threshold(value.get("warn_at"), warn, f"tier_thresholds.{key}.warn_at")
        if t_warn > t_good:
            raise rpt.ReportError(f"outage.tier_thresholds.{key}: warn_at must not be higher than good_at")
        tiers[tier] = (t_good, t_warn)
    return {"good": good, "warn": warn, "tiers": tiers}


def load_thresholds():
    """(thresholds, problem). A bad settings.json gives the defaults and a sentence saying so."""
    try:
        return parse_thresholds(settings_loader.get_section("outage")), None
    except Exception as exc:
        return parse_thresholds(None), f"Availability colors use the built-in defaults because of a settings.json problem: {exc}"


def level(value, tier, thresholds):
    """'good', 'warn', 'bad' or 'none' for an availability percentage (compared as displayed, 3 decimals)."""
    if value is None:
        return "none"
    good, warn = thresholds["tiers"].get(tier, (thresholds["good"], thresholds["warn"]))
    shown = round(value, 3)
    if shown >= good:
        return "good"
    if shown >= warn:
        return "warn"
    return "bad"


def _cut_text(thresholds):
    text = f"green at {thresholds['good']:g}% or above, amber from {thresholds['warn']:g}%, red below that"
    if thresholds["tiers"]:
        names = ", ".join(f"{TIER_NAMES[t]} {g:g}/{w:g}" for t, (g, w) in sorted(thresholds["tiers"].items()))
        text += f" (own cut-offs: {names})"
    return text


# ---------------------------------------------------------------------------
# loading (computed once, shared by every page of a run)
# ---------------------------------------------------------------------------

_cache = {"sig": None, "at": 0.0, "data": None}


def _signature(conn):
    parts = []
    pull = outage_db.last_pull(conn)
    parts.append(repr(sorted(dict(pull).items())) if pull is not None else "no-pull")
    parts.append(repr([(w["id"], str(w["start"]), str(w["end"]), w["sites"]) for w in outage_db.load_planned_windows(conn)]))
    parts.append(repr([(s["site_octet"], s["tier"], s["site_name"]) for s in db.get_all_sites(conn, include_unassigned=False)]))
    try:
        parts.append(json.dumps(settings_loader.get_section("outage"), sort_keys=True, default=str))
    except Exception as exc:
        parts.append("settings-error " + str(exc))
    return "|".join(parts)


def _data(conn):
    """{"months": [((y, m), report), ...] newest first, "pull", "th", "th_note", "error"}"""
    sig = _signature(conn)
    if _cache["sig"] == sig and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    th, th_note = load_thresholds()
    data = {"months": [], "pull": None, "th": th, "th_note": th_note, "error": None}
    pull = outage_db.last_pull(conn)
    if pull is not None:
        data["pull"] = pull
        try:
            cfg = rpt.load_settings()
            planned = outage_db.load_planned_windows(conn)
            reports = {}
            for ym in rpt.months_covered(conn)[-MAX_MONTHS:]:
                reports[ym] = rpt.compute_month(conn, ym[0], ym[1], settings=cfg, planned=planned)
            data["months"] = sorted(reports.items(), reverse=True)
        except rpt.ReportError as exc:
            data["error"] = str(exc)
    _cache.update(sig=sig, at=time.monotonic(), data=data)
    return data


def _full_core_minutes(cfg, start, end):
    per_day = cfg["core_end_minute"] - cfg["core_start_minute"]
    total, day = 0, start
    while day < end:
        if day.weekday() in cfg["core_days"]:
            total += per_day
        day += datetime.timedelta(days=1)
    return total


def _meta(rep):
    start, end = rep["window"]
    now = op.parse_time(rep["last_pull"]["server_now"])
    return {"start": start, "end": end, "now": now, "in_progress": now < end, "key": f"{start:%Y-%m}",
            "label": f"{start:%B %Y}", "short": f"{start:%b %Y}",
            "counted": rep["calc"]["coverage"]["core_minutes"],
            "full": _full_core_minutes(rep["settings"], start, end)}


# ---------------------------------------------------------------------------
# small formatting helpers
# ---------------------------------------------------------------------------

def _pct(value):
    return "n/a" if value is None else f"{value:.3f}%"


def _mins(value):
    return f"{value:,.0f}"


def _when(dt):
    return f"{dt:%b} {dt.day} {dt:%H:%M}"


def _n(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _cls(lv):
    return LEVEL_CLASS[lv]


def _tier_text(tier):
    return TIER_NAMES.get(tier, "Untiered")


def _site_planned(rep, octet):
    return [w for w in rep["planned"] if w["sites"] is None or octet in w["sites"]]


def _touches(w, meta):
    return w["start"] < meta["end"] and w["end"] > meta["start"]


def _scope_text(w):
    if w["sites"] is None:
        return "all sites"
    if len(w["sites"]) == 1:
        return "this site only"
    return f"{len(w['sites'])} sites"


# ---------------------------------------------------------------------------
# the site page panel
# ---------------------------------------------------------------------------

PANEL_CSS = """
.ou-good{ color:var(--up); } .ou-warn{ color:var(--accent); } .ou-bad{ color:var(--warn); } .ou-none{ color:var(--text-dim); }
.ou-body{ padding:14px; display:flex; flex-direction:column; gap:14px; }
.ou-month{ display:flex; flex-direction:column; gap:14px; }
.ou-month[hidden]{ display:none; }
.ou-top{ display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; justify-content:space-between; }
.ou-top label{ font-family:var(--mono); font-size:11px; color:var(--text-dim); margin-right:6px; }
.ou-top select{ background:var(--bg); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:var(--mono); font-size:12px; padding:6px 10px; }
.ou-figs{ display:flex; flex-wrap:wrap; gap:10px; }
.ou-fig{ flex:1 1 170px; min-width:0; background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:10px 12px; display:flex; flex-direction:column; gap:3px; }
.ou-fig .t{ font-family:var(--mono); font-size:10.5px; text-transform:uppercase; letter-spacing:.05em; color:var(--text-dim); }
.ou-fig .v{ font-family:var(--mono); font-size:20px; font-weight:700; font-variant-numeric:tabular-nums; }
.ou-fig .s{ font-size:11.5px; color:var(--text-dim); }
.ou-note{ font-family:var(--mono); font-size:11.5px; color:var(--text-dim); line-height:1.5; }
.ou-note.amber{ color:var(--accent); }
.ou-strip{ position:relative; height:34px; background:var(--bg); border:1px solid var(--border); border-radius:6px; overflow:hidden; }
.ou-strip div{ position:absolute; top:0; bottom:0; }
.ou-core{ background:var(--panel-2); }
.ou-plan{ background:color-mix(in srgb, #5aa9e6 30%, transparent); border-left:1px solid #5aa9e6; border-right:1px solid #5aa9e6; }
.ou-nodata{ background:repeating-linear-gradient(45deg, transparent 0 4px, var(--border) 4px 5px); }
.ou-mark{ top:6px !important; bottom:6px !important; min-width:3px; border-radius:2px; background:var(--warn); }
.ou-mark.pl{ background:#5aa9e6; }
.ou-last{ width:2px; background:var(--text); }
.ou-legend{ display:flex; flex-wrap:wrap; gap:6px 16px; font-family:var(--mono); font-size:11px; color:var(--text-dim); }
.ou-legend i{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; vertical-align:-1px; }
.ou-scroll{ overflow-x:auto; }
table.ou-t{ width:100%; border-collapse:collapse; font-family:var(--mono); font-size:12px; font-variant-numeric:tabular-nums; }
table.ou-t th{ text-align:left; font-weight:600; font-size:10.5px; text-transform:uppercase; letter-spacing:.05em; color:var(--text-dim); padding:7px 10px; white-space:nowrap; }
table.ou-t td{ padding:7px 10px; border-top:1px solid var(--border); }
table.ou-t th.r, table.ou-t td.r{ text-align:right; }
table.ou-t tr:hover td{ background:var(--panel-2); }
.ou-pill{ font-family:var(--mono); font-size:10.5px; border-radius:999px; padding:2px 9px; border:1px solid var(--border); white-space:nowrap; }
.ou-pill.bad{ color:var(--warn); border-color:var(--warn); } .ou-pill.warn{ color:var(--accent); border-color:var(--accent); } .ou-pill.plan{ color:#5aa9e6; border-color:#5aa9e6; }
.ou-sub{ font-family:var(--mono); font-size:10.5px; text-transform:uppercase; letter-spacing:.06em; color:var(--text-dim); margin:0 0 6px; }
.ou-list{ display:flex; flex-direction:column; gap:6px; }
.ou-item{ display:flex; flex-wrap:wrap; justify-content:space-between; gap:4px 12px; font-family:var(--mono); font-size:12px; padding:7px 12px; border:1px solid var(--border); border-radius:8px; background:var(--panel-2); }
.ou-item .dim{ color:var(--text-dim); }
"""

PANEL_JS = """
document.querySelectorAll(".ou-month-pick").forEach(function(sel){
  sel.addEventListener("change", function(){
    var panel = sel.closest(".ou-panel");
    panel.querySelectorAll(".ou-month").forEach(function(b){ b.hidden = (b.dataset.month !== sel.value); });
  });
});
"""


def _strip(rep, meta, entry, planned_here):
    total = (meta["end"] - meta["start"]).total_seconds() / 60.0

    def pos(dt):
        return max(0.0, min(100.0, (dt - meta["start"]).total_seconds() / 60.0 / total * 100.0))

    cfg = rep["settings"]
    parts = []
    day = meta["start"]
    while day < meta["end"]:
        if day.weekday() in cfg["core_days"]:
            a = day + datetime.timedelta(minutes=cfg["core_start_minute"])
            b = day + datetime.timedelta(minutes=cfg["core_end_minute"])
            parts.append(f'<div class="ou-core" style="left:{pos(a):.3f}%;width:{pos(b) - pos(a):.3f}%"></div>')
        day += datetime.timedelta(days=1)
    cursor, horizon = meta["start"], min(meta["end"], meta["now"])
    for a, b in rep["calc"]["coverage"]["spans"]:
        if a > cursor:
            parts.append(f'<div class="ou-nodata" style="left:{pos(cursor):.3f}%;width:{pos(a) - pos(cursor):.3f}%"></div>')
        cursor = max(cursor, b)
    if cursor < horizon:
        parts.append(f'<div class="ou-nodata" style="left:{pos(cursor):.3f}%;width:{pos(horizon) - pos(cursor):.3f}%"></div>')
    for w in planned_here:
        a, b = max(w["start"], meta["start"]), min(w["end"], meta["end"])
        parts.append(f'<div class="ou-plan" title="Planned window #{w["id"]}" style="left:{pos(a):.3f}%;width:{pos(b) - pos(a):.3f}%"></div>')
    for o in entry["outages"]:
        end_dt = meta["now"] if o["ongoing"] else o["end"]
        unplanned = o["core_unplanned"] + o["outside_unplanned"]
        kind = "ou-mark" if unplanned > 0 else "ou-mark pl"
        tip = _esc(f"{_when(o['start'])} to " + ("ongoing" if o["ongoing"] else _when(o["end"])))
        width = max(pos(end_dt) - pos(o["start"]), 0.25)
        parts.append(f'<div class="{kind}" title="{tip}" style="left:{pos(o["start"]):.3f}%;width:{width:.3f}%"></div>')
    if meta["in_progress"]:
        parts.append(f'<div class="ou-last" title="Last pull" style="left:{pos(meta["now"]):.3f}%"></div>')
    return '<div class="ou-strip">' + "".join(parts) + "</div>"


def _site_month_block(octet, rep, th, first):
    meta = _meta(rep)
    entry = rep["calc"]["sites"].get(octet)
    hidden = "" if first else " hidden"
    head = f'<div class="ou-month" data-month="{meta["key"]}"{hidden}>'
    if entry is None:
        return head + '<p class="ou-note">No outage data for this site in this month.</p></div>'
    if not entry["tracked"]:
        return head + f'<p class="ou-note amber">Outages are not tracked for this site: {_esc(entry["reason"])}.</p></div>'
    tier = rep["tiers"].get(octet)
    lv_all = level(entry["availability_all"], tier, th)
    lv_ex = level(entry["availability_excl_planned"], tier, th)
    outs = entry["outages"]
    ongoing = sum(1 for o in outs if o["ongoing"])
    planned_here = [w for w in _site_planned(rep, octet) if _touches(w, meta)]
    figs = [
        ("Availability, all downtime", _pct(entry["availability_all"]), _cls(lv_all), f"{_mins(entry['down']['core_unplanned'] + entry['down']['core_planned'])} core minutes down"),
        ("Availability, planned excluded", _pct(entry["availability_excl_planned"]), _cls(lv_ex), f"{_mins(entry['down']['core_planned'])} planned core minutes removed"),
        ("Outages", str(len(outs)), "ou-bad" if ongoing else "", f"{ongoing} ongoing" if ongoing else "none ongoing"),
        ("Core minutes counted", _mins(meta["counted"]), "", f"of {_mins(meta['full'])} in the month" if meta["in_progress"] else "whole month"),
    ]
    figs_html = "".join(f'<div class="ou-fig"><div class="t">{t}</div><div class="v {c}">{v}</div><div class="s">{_esc(s)}</div></div>' for t, v, c, s in figs)
    notes = []
    if meta["in_progress"]:
        notes.append(f'<p class="ou-note amber">Month in progress: availability so far, data current through {_when(meta["now"])} (Orion time).</p>')
    for w in rep["warnings"]:
        if not w.startswith("The month is not over"):
            notes.append(f'<p class="ou-note">{_esc(w)}</p>')
    legend = ('<div class="ou-legend"><span><i style="background:var(--warn)"></i>Unplanned</span>'
              '<span><i style="background:#5aa9e6"></i>Planned</span>'
              '<span><i style="background:var(--panel-2);border:1px solid var(--border)"></i>Core hours</span>'
              '<span><i style="background:repeating-linear-gradient(45deg,transparent 0 2px,var(--border) 2px 3px);border:1px solid var(--border)"></i>No data</span></div>')
    if outs:
        rows = []
        for o in outs:
            outside = o["outside_unplanned"] + o["outside_planned"]
            ids = ", ".join(f"#{i}" for i in o["planned_ids"] if i is not None)
            end_text = '<span class="ou-bad">ongoing</span>' if o["ongoing"] else _when(o["end"])
            rows.append(
                f'<tr><td>{_when(o["start"])}</td><td>{end_text}</td><td class="r">{_mins(o["minutes"])} min</td>'
                f'<td class="r">{_mins(o["core_unplanned"])} min</td><td class="r" style="color:#5aa9e6">{_mins(o["core_planned"])} min</td>'
                f'<td class="r ou-none">{_mins(outside)} min</td><td class="ou-none">{_esc(outage_calc.describe_parts(o))}</td>'
                f'<td style="color:#5aa9e6">{_esc(ids)}</td></tr>')
        table = ('<div class="ou-scroll"><table class="ou-t"><thead><tr><th>Start</th><th>End</th><th class="r">Length</th>'
                 '<th class="r">Core unplanned</th><th class="r">Core planned</th><th class="r">Outside core</th>'
                 '<th>How it was built</th><th>Planned</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>")
    else:
        table = '<p class="ou-note">No outages this month.</p>'
    devs = ""
    if entry["device_outages"]:
        items = "".join(
            f'<div class="ou-item"><span>{_esc(d["hostname"] or "device")}</span><span class="dim">{_when(d["start"])} to '
            f'{"ongoing" if d["ongoing"] else _when(d["end"])}</span><span>{_mins(d["minutes"])} min</span></div>'
            for d in entry["device_outages"])
        devs = f'<div><p class="ou-sub">Other devices at this site (site was up; not counted in availability)</p><div class="ou-list">{items}</div></div>'
    plans = ""
    if planned_here:
        items = "".join(
            f'<div class="ou-item"><span style="color:#5aa9e6">#{w["id"]} {_esc(w.get("reason") or "planned window")}</span>'
            f'<span class="dim">{_when(w["start"])} to {_when(w["end"])}</span><span class="dim">{_scope_text(w)}</span></div>'
            for w in planned_here)
        plans = f'<div><p class="ou-sub">Planned windows touching this month</p><div class="ou-list">{items}</div></div>'
    return (head + f'<div class="ou-figs">{figs_html}</div>' + "".join(notes) + _strip(rep, meta, entry, planned_here)
            + legend + table + devs + plans + "</div>")


def render_site_panel(conn, site_row):
    """The collapsed "Outages" panel for one site page, or "" when there is nothing to show."""
    octet = site_row["site_octet"]
    if octet == db.UNASSIGNED_SITE_OCTET:
        return ""
    data = _data(conn)
    if data["error"]:
        return ('<div class="panel"><div class="panel-head"><span class="label">Outages</span></div>'
                f'<p class="ou-note" style="padding:12px 14px">Outages could not be worked out: {_esc(data["error"])}</p></div>')
    if not data["months"] or all(rep["calc"]["sites"].get(octet) is None for _, rep in data["months"]):
        return ""
    th = data["th"]
    first_rep = data["months"][0][1]
    first_entry = first_rep["calc"]["sites"].get(octet)
    count = ""
    if first_entry is not None and first_entry["tracked"]:
        tier = first_rep["tiers"].get(octet)
        lv = level(first_entry["availability_all"], tier, th)
        count = (f'{len(first_entry["outages"])} outage(s) in {_meta(first_rep)["short"]} &middot; '
                 f'<span class="{_cls(lv)}">{_pct(first_entry["availability_all"])}</span>')
    elif first_entry is not None:
        count = "not tracked"
    options = "".join(f'<option value="{_meta(rep)["key"]}">{_meta(rep)["label"]}</option>' for _, rep in data["months"])
    blocks = "".join(_site_month_block(octet, rep, th, i == 0) for i, (_, rep) in enumerate(data["months"]))
    pick = ""
    if len(data["months"]) > 1:
        pick = f'<div class="ou-top"><span><label for="ou-pick-{_esc(octet)}">Month</label><select class="ou-month-pick" id="ou-pick-{_esc(octet)}">{options}</select></span></div>'
    link = '<a href="outages.html" style="font-family:var(--mono);font-size:11.5px;color:var(--text-dim)">All sites &rarr;</a>'
    cut = f'<p class="ou-note">Availability is {_esc(_cut_text(th))}. Core hours only; planned downtime shown separately.</p>'
    th_note = f'<p class="ou-note amber">{_esc(data["th_note"])}</p>' if data["th_note"] else ""
    return (f'<details class="panel ou-panel"><summary class="panel-head"><span class="label"><span class="chevron">&#9656;</span>Outages</span>'
            f'<span class="count">{count}</span></summary><div class="ou-body">{pick}{blocks}{cut}{th_note}{link}</div></details>')


# ---------------------------------------------------------------------------
# the index page tile
# ---------------------------------------------------------------------------

def summary(conn):
    """Figures for the index tile for the newest stored month, or None."""
    data = _data(conn)
    if data["error"] or not data["months"]:
        return None
    rep = data["months"][0][1]
    meta = _meta(rep)
    tiers = rpt.tier_summary(rep)
    overall = tiers[-1]
    return {"label": meta["short"], "in_progress": meta["in_progress"], "sites": overall["sites"],
            "avg_all": overall["avg_all"], "outages": overall["outages"],
            "without": overall["sites_without_outage"],
            "ongoing": sum(1 for e in rep["calc"]["sites"].values() if e["tracked"] for o in e["outages"] if o["ongoing"]),
            "level": level(overall["avg_all"], None, data["th"])}


def render_index_bits(conn):
    """(button_html, tile_html) for the index page; both empty when there is no outage data."""
    try:
        s = summary(conn)
    except Exception:
        return "", ""
    if s is None:
        return "", ""
    button = '<a class="btn" href="outages.html">Outages</a>'
    cls = {"good": "num", "warn": "num amber", "bad": "num warn", "none": "num"}[s["level"]]
    sub = f'{s["ongoing"]} ongoing' if s["ongoing"] else f'{s["outages"]} outage(s)'
    label = f'Outages {s["label"]}{" so far" if s["in_progress"] else ""} &middot; {sub}'
    tile = (f'<a class="kpi kpi-link" href="outages.html" title="Site outages"><center><div class="{cls}">{_pct(s["avg_all"])}</div>'
            f'<div class="label">{label}</div></center></a>')
    return button, tile


# ---------------------------------------------------------------------------
# the fleet page
# ---------------------------------------------------------------------------

FLEET_CSS = """
.ou-bar{ display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; justify-content:space-between; margin-bottom:14px; }
.ou-bar select{ background:var(--panel); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:var(--mono); font-size:13px; padding:8px 12px; }
.ou-bar label{ font-family:var(--mono); font-size:12px; color:var(--text-dim); margin-right:8px; }
.ou-notice{ border:1px solid var(--accent-dim); background:var(--panel-2); border-radius:8px; padding:9px 14px; font-family:var(--mono); font-size:12px; color:var(--accent); margin-bottom:10px; }
.ou-strip-tiers{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:18px; }
.ou-tcard{ flex:1 1 150px; min-width:0; background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); padding:11px 14px; display:flex; flex-direction:column; gap:3px; }
.ou-tcard .h{ display:flex; justify-content:space-between; gap:8px; font-family:var(--mono); font-size:12px; }
.ou-tcard .h b{ font-weight:700; }
.ou-tcard .v{ font-family:var(--mono); font-size:19px; font-weight:700; font-variant-numeric:tabular-nums; }
.ou-tcard .s{ font-family:var(--mono); font-size:11px; color:var(--text-dim); font-variant-numeric:tabular-nums; }
.ou-tcard.empty{ opacity:.75; border-style:dashed; }
details.ou-group{ background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; margin-bottom:14px; }
details.ou-group > summary{ list-style:none; cursor:pointer; background:var(--panel-2); border-bottom:1px solid var(--border); padding:10px 14px; display:flex; flex-wrap:wrap; gap:8px 16px; justify-content:space-between; align-items:center; }
details.ou-group > summary::-webkit-details-marker{ display:none; }
.ou-gname{ font-family:var(--mono); font-size:13px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; }
.ou-gnote{ font-size:12px; color:var(--text-dim); margin-left:10px; }
.ou-chips{ display:flex; flex-wrap:wrap; gap:6px; }
.ou-chip{ font-family:var(--mono); font-size:11px; background:var(--bg); border:1px solid var(--border); border-radius:999px; padding:2px 9px; color:var(--text-dim); font-variant-numeric:tabular-nums; }
.ou-foot{ border-top:1px solid var(--border); padding:8px 14px; font-family:var(--mono); font-size:11px; color:var(--text-dim); }
table.ou-t a{ text-decoration:none; color:var(--accent); } table.ou-t a:hover{ text-decoration:underline; }
"""


def _tier_groups(rep):
    groups = {}
    for octet, entry in rep["calc"]["sites"].items():
        if entry["tracked"]:
            groups.setdefault(rep["tiers"].get(octet), []).append((octet, entry))
    return groups


def _summarise(entries):
    avail = [e["availability_all"] for e in entries if e["availability_all"] is not None]
    excl = [e["availability_excl_planned"] for e in entries if e["availability_excl_planned"] is not None]
    return {"sites": len(entries), "outages": sum(len(e["outages"]) for e in entries),
            "avg_all": sum(avail) / len(avail) if avail else None,
            "avg_excl_planned": sum(excl) / len(excl) if excl else None}


def _tier_summaries(groups):
    """{tier: figures, ..., "all": figures}. Keyed by the tier number, so it never depends on tier names."""
    out = {tier: _summarise([e for _, e in members]) for tier, members in groups.items()}
    out["all"] = _summarise([e for members in groups.values() for _, e in members])
    return out


def _row_html(octet, entry, tier, th):
    lv_all = level(entry["availability_all"], tier, th)
    lv_ex = level(entry["availability_excl_planned"], tier, th)
    ongoing = any(o["ongoing"] for o in entry["outages"])
    down = entry["down"]
    if ongoing:
        pill = '<span class="ou-pill bad">ongoing</span>'
    elif down["core_unplanned"] == 0 and down["outside_unplanned"] == 0 and entry["outages"]:
        pill = '<span class="ou-pill plan">planned only</span>'
    elif lv_all == "bad":
        pill = '<span class="ou-pill bad">below target</span>'
    else:
        pill = ""
    name = _esc(entry["site_name"] or f"Site {octet}")
    return (f'<tr><td><a href="site-{_esc(octet)}.html">{_esc(octet)}</a></td><td>{name}</td><td class="r">{len(entry["outages"])}</td>'
            f'<td class="r">{_mins(down["core_unplanned"])} min</td><td class="r" style="color:#5aa9e6">{_mins(down["core_planned"])} min</td>'
            f'<td class="r {_cls(lv_all)}"><b>{_pct(entry["availability_all"])}</b></td>'
            f'<td class="r {_cls(lv_ex)}"><b>{_pct(entry["availability_excl_planned"])}</b></td><td>{pill}</td></tr>')


def render_fleet_page(conn, ym):
    """The outage page for one stored month (ym = (year, month))."""
    import dashboard_generate as dg   # shared theme tokens; imported late to avoid a circular import
    data = _data(conn)
    th = data["th"]
    reports = dict(data["months"])
    rep = reports[ym]
    meta = _meta(rep)
    groups = _tier_groups(rep)
    summaries = _tier_summaries(groups)
    overall = summaries["all"]
    ongoing = sum(1 for _, e in (x for g in groups.values() for x in g) for o in e["outages"] if o["ongoing"])
    tracked = overall["sites"]

    xlsx_name = f"outage_report_{meta['key']}.xlsx"
    excel = (f'<a class="btn" href="outage_reports/{xlsx_name}" download>Download Excel</a>'
             if os.path.exists(os.path.join(REPORT_DIR, xlsx_name)) else "")
    options = "".join(
        f'<option value="outages-{_meta(r)["key"]}.html"{" selected" if key == ym else ""}>{_meta(r)["label"]}{" (in progress)" if _meta(r)["in_progress"] else ""}</option>'
        for key, r in data["months"])

    notices = []
    if meta["in_progress"]:
        notices.append(f'The month is not over: availability so far, {_mins(meta["counted"])} of {_mins(meta["full"])} core minutes counted '
                       f'(data current through {_when(meta["now"])} Orion time).')
    notices += [w for w in rep["warnings"] if not w.startswith("The month is not over")]
    if data["th_note"]:
        notices.append(data["th_note"])
    notice_html = "".join(f'<div class="ou-notice">{_esc(n)}</div>' for n in notices)

    lv_all, lv_ex = level(overall["avg_all"], None, th), level(overall["avg_excl_planned"], None, th)
    kpis = [
        (str(tracked), "num", "Tracked sites"),
        (str(overall["outages"]), "num warn" if ongoing else "num", f"Outages &middot; {ongoing} ongoing"),
        (_pct(overall["avg_all"]), "num " + {"good": "", "warn": "amber", "bad": "warn", "none": ""}[lv_all], "Availability, all downtime"),
        (_pct(overall["avg_excl_planned"]), "num " + {"good": "", "warn": "amber", "bad": "warn", "none": ""}[lv_ex], "Availability, planned excluded"),
    ]
    kpi_html = "".join(f'<div class="kpi"><div class="{c}">{v}</div><div class="label">{l}</div></div>' for v, c, l in kpis)

    card_order = [t for t in sorted(t for t in groups if t is not None)] + [None]
    cards = []
    for tier in card_order:
        s = summaries.get(tier)
        empty = s is None
        lv = "none" if empty else level(s["avg_all"], tier, th)
        cards.append(
            f'<div class="ou-tcard{" empty" if empty else ""}"><div class="h"><b>{_tier_text(tier)}</b><span class="ou-none">{_n(0 if empty else s["sites"], "site")}</span></div>'
            f'<div class="v {_cls(lv)}">{"n/a" if empty else _pct(s["avg_all"])}</div>'
            f'<div class="s">{"no sites without a tier" if empty else _pct(s["avg_excl_planned"]) + " planned excluded"}</div></div>')
    lv_o = level(overall["avg_all"], None, th)
    cards.append(f'<div class="ou-tcard"><div class="h"><b>All sites</b><span class="ou-none">{_n(tracked, "site")}</span></div>'
                 f'<div class="v {_cls(lv_o)}">{_pct(overall["avg_all"])}</div><div class="s">{_pct(overall["avg_excl_planned"])} planned excluded</div></div>')

    group_html = []
    for tier in card_order:
        if tier not in groups:
            continue
        s = summaries[tier]
        members = sorted(groups[tier], key=lambda m: rpt._site_sort_key(m[0]))
        with_outage = [m for m in members if m[1]["outages"]]
        lv = level(s["avg_all"], tier, th)
        note = TIER_NOTES.get(tier, "")
        note_html = f'<span class="ou-gnote">{_esc(note)}</span>' if note else ""
        body = "".join(_row_html(o, e, tier, th) for o, e in with_outage)
        table = (f'<div class="ou-scroll"><table class="ou-t"><thead><tr><th>Site</th><th>Name</th><th class="r">Outages</th>'
                 f'<th class="r">Core unplanned</th><th class="r">Core planned</th><th class="r">All downtime</th>'
                 f'<th class="r">Planned excluded</th><th>Status</th></tr></thead><tbody>{body}</tbody></table></div>'
                 if with_outage else '<p class="ou-note" style="padding:12px 14px">No outages in this group this month.</p>')
        clean = s["sites"] - len(with_outage)
        foot = f'<div class="ou-foot">{_n(clean, "other site")} in this group had no outage and {"is" if clean == 1 else "are"} not listed.</div>' if clean else ""
        group_html.append(
            f'<details class="ou-group" open><summary><span><span class="ou-gname">{_tier_text(tier)}</span>{note_html}</span>'
            f'<span class="ou-chips"><span class="ou-chip">{_n(s["sites"], "site")}</span>'
            f'<span class="ou-chip {_cls(lv)}">avg {_pct(s["avg_all"])}</span>'
            f'<span class="ou-chip">{_pct(s["avg_excl_planned"])} planned excluded</span>'
            f'<span class="ou-chip">{clean} with no outage</span></span></summary>{table}'
            f'{foot}</details>')

    skipped = [(o, e) for o, e in rep["calc"]["sites"].items() if not e["tracked"]]
    skipped.sort(key=lambda m: rpt._site_sort_key(m[0]))
    skipped_html = ""
    if skipped:
        rows = "".join(f'<tr><td><a href="site-{_esc(o)}.html">{_esc(o)}</a></td><td>{_esc(e["site_name"] or "")}</td><td class="ou-none">{_esc(e["reason"])}</td></tr>' for o, e in skipped)
        skipped_html = (f'<details class="ou-group"><summary><span><span class="ou-gname">Not tracked</span>'
                        f'<span class="ou-gnote">No availability figure</span></span><span class="ou-chips"><span class="ou-chip">{_n(len(skipped), "site")}</span></span></summary>'
                        f'<div class="ou-scroll"><table class="ou-t"><thead><tr><th>Site</th><th>Name</th><th>Reason</th></tr></thead><tbody>{rows}</tbody></table></div></details>')

    cfg = rep["settings"]
    days = ", ".join(rpt.DAY_NAMES[d] for d in cfg["core_days"])
    core_text = f'{days} {cfg["core_start_minute"] // 60:02d}:{cfg["core_start_minute"] % 60:02d}-{cfg["core_end_minute"] // 60:02d}:{cfg["core_end_minute"] % 60:02d}'
    rules = (f'Orion node events, Eastern time. Core hours {core_text}. An outage counts after {cfg["min_down_minutes"]} minutes down; '
             f'outages with less than {cfg["up_interval_minutes"]} minutes up between them merge. Availability is {_cut_text(th)}.')
    pulled = f"{meta['now']:%Y-%m-%d %H:%M}"
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f'''<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Site Outages {_esc(meta["label"])} - Site Awareness Dashboard</title>
<style>
{dg.CSS_TOKENS}
*{{ box-sizing: border-box; }}
body{{ margin:0; background:var(--bg); color:var(--text); font-family:var(--sans); -webkit-font-smoothing:antialiased; }}
.wrap{{ max-width:1200px; margin:0 auto; padding:24px 24px 60px; }}
a{{ color:inherit; }}
.backlink{{ display:inline-block; font-family:var(--mono); font-size:12px; color:var(--text-dim); border:1px solid var(--border); border-radius:999px; padding:5px 12px; text-decoration:none; margin-bottom:14px; }}
.backlink:hover{{ color:var(--accent); border-color:var(--accent); }}
header{{ display:flex; justify-content:space-between; align-items:flex-end; border-bottom:2px solid var(--border); padding-bottom:16px; margin-bottom:20px; gap:16px; flex-wrap:wrap; }}
header h1{{ margin:0; font-size:22px; font-weight:700; }}
.subtitle{{ font-family:var(--mono); font-size:12px; color:var(--text-dim); margin-top:4px; }}
button, .btn{{ background:var(--panel-2); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:var(--sans); font-size:13px; padding:8px 14px; cursor:pointer; text-decoration:none; display:inline-block; }}
button:hover, .btn:hover{{ border-color:var(--accent); }}
.icon-btn{{ width:38px; height:38px; padding:0; display:flex; align-items:center; justify-content:center; font-size:16px; }}
.header-actions{{ display:flex; gap:8px; align-items:center; }}
.kpi-grid{{ display:flex; flex-wrap:wrap; justify-content:center; gap:12px; margin-bottom:20px; }}
.kpi{{ flex:0 1 220px; background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); padding:14px 16px; text-align:center; display:flex; flex-direction:column; justify-content:center; }}
.kpi .num{{ font-family:var(--mono); font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }}
.kpi .num.warn{{ color:var(--warn); }} .kpi .num.amber{{ color:var(--accent); }}
.kpi .label{{ font-family:var(--mono); font-size:11px; text-transform:uppercase; letter-spacing:.07em; color:var(--text-dim); margin-top:4px; }}
{PANEL_CSS}
{FLEET_CSS}
footer{{ margin-top:20px; font-family:var(--mono); font-size:11px; color:var(--text-dim); text-align:center; line-height:1.6; }}
{banner.CSS}
</style>
</head>
<body>
{banner.html()}
<div class="wrap">
  <a class="backlink" href="index.html">&larr; All Sites</a>
  <header>
    <div>
      <h1>Site outages</h1>
      <div class="subtitle">{_esc(meta["label"])} &middot; last Orion pull {_esc(pulled)} &middot; generated {generated_at}</div>
    </div>
    <div class="header-actions">
      {excel}
      <button class="icon-btn" id="theme-toggle" title="Toggle theme">\U0001F319</button>
    </div>
  </header>

  <div class="ou-bar">
    <span><label for="month-pick">Month</label><select id="month-pick">{options}</select></span>
  </div>
  {notice_html}
  <div class="kpi-grid">{kpi_html}</div>
  <div class="ou-strip-tiers">{"".join(cards)}</div>
  {"".join(group_html)}
  {skipped_html}
  <footer>{_esc(rules)}<br>Only sites with at least one outage are listed. Generated from sad.db, read-only.</footer>
</div>
<script>
document.getElementById("month-pick").addEventListener("change", function(){{ window.location.href = this.value; }});
{dg.THEME_TOGGLE_JS}
</script>
</body>
</html>
'''


# ---------------------------------------------------------------------------
# writing files
# ---------------------------------------------------------------------------

def _write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _write_snapshots(data):
    """One Excel file per stored month. A month still in progress is rewritten every run;
    a finished month is written once more and then left alone."""
    os.makedirs(REPORT_DIR, exist_ok=True)
    manifest_path = os.path.join(REPORT_DIR, "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        if not isinstance(manifest, dict):
            manifest = {}
    except (OSError, ValueError):
        manifest = {}
    written = []
    for _, rep in data["months"]:
        meta = _meta(rep)
        path = os.path.join(REPORT_DIR, f"outage_report_{meta['key']}.xlsx")
        if os.path.exists(path) and manifest.get(meta["key"]) == "final":
            continue
        tmp = path + ".tmp"
        try:
            xlsx_writer.write_xlsx(tmp, rpt.build_sheets(rep))
            os.replace(tmp, path)
        except Exception as exc:
            print(f"  Could not write {path}: {exc}", file=sys.stderr)
            continue
        manifest[meta["key"]] = "partial" if meta["in_progress"] else "final"
        written.append(path)
    _write_text(manifest_path, json.dumps(manifest, indent=1, sort_keys=True))
    return written


def generate_outage_pages(conn):
    """Write the outage pages and monthly Excel files. Returns the list of paths written
    (empty when nothing has been pulled yet)."""
    data = _data(conn)
    if data["error"] or not data["months"]:
        return []
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    paths = _write_snapshots(data)
    pages = [(f"outages-{_meta(rep)['key']}.html", ym) for ym, rep in data["months"]]
    pages.append(("outages.html", data["months"][0][0]))
    for name, ym in pages:
        path = os.path.join(OUTPUT_DIR, name)
        try:
            _write_text(path, render_fleet_page(conn, ym))
        except Exception as exc:
            raise RuntimeError(f"could not build {name}: {type(exc).__name__}: {exc}") from exc
        paths.append(path)
    return paths