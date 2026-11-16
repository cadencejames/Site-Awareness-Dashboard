"""
outage_db.py - database side of SAD's site-outage tracking.

This is db.py's companion for everything about Orion events and outages, kept
in its own file so db.py stays focused on discovery data. The same rules apply:
nothing outside db.py and this file writes SQL against these tables, and every
write function takes `conn` first so it can be queued through write_queue.py.

Tables
  orion_nodes   every Orion node seen in a pull, and the SAD device it matched (if any)
  orion_events  every node Down/Up event pulled, keyed by Orion's own EventID so
                pulling the same event twice never makes a duplicate
  orion_pulls   one row per pull: when, what period it covered, what it fetched
  outage_windows / outage_window_sites
                planned downtime windows (all sites, or a list of site octets),
                entered by hand. Deleting one is soft, so it can be restored.
                `kind` is 'planned' today; manual outage entries will share the table.

Also here: the site `tier` (0 = most important ... 3 = least; 4 = sites that go offline often because of their own operations), a nullable
column on sites. NULL means "not tiered yet".

Event times are stored exactly as Orion reports them: Eastern wall-clock time as
text ('YYYY-MM-DD HH:MM:SS[.ffffff]'), which sorts correctly as text. They are
NOT converted to UTC here; outage_calc does that, and uses the EventID order to
settle the hour that repeats when daylight saving ends.

orion_nodes.device_id and orion_events.node_id are plain numbers with no foreign
keys on purpose: SAD hard-deletes devices when merging duplicates, and history
must survive that. A node that stops matching keeps its last known device_id; a
node that matches a different device later takes the new one.
"""
from datetime import datetime

import db
import orion_parser as op

SCHEMA = """
CREATE TABLE IF NOT EXISTS orion_nodes (
    node_id      INTEGER PRIMARY KEY,
    caption      TEXT,
    ip           TEXT,
    status       INTEGER,
    device_id    INTEGER,
    match_method TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orion_events (
    event_id   INTEGER PRIMARY KEY,
    node_id    INTEGER NOT NULL,
    event_time TEXT NOT NULL,
    event_type INTEGER NOT NULL,
    pulled_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orion_pulls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pulled_at      TEXT NOT NULL,
    pulled_by      TEXT,
    server_now     TEXT NOT NULL,
    days           INTEGER NOT NULL,
    covered_from   TEXT NOT NULL,
    covered_to     TEXT NOT NULL,
    earliest_event TEXT,
    events_fetched INTEGER NOT NULL,
    events_new     INTEGER NOT NULL,
    nodes_fetched  INTEGER NOT NULL,
    nodes_matched  INTEGER NOT NULL,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS outage_windows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL DEFAULT 'planned',
    all_sites  INTEGER NOT NULL DEFAULT 0,
    start_time TEXT NOT NULL,
    end_time   TEXT NOT NULL,
    reason     TEXT,
    source     TEXT NOT NULL DEFAULT 'manual',
    created_by TEXT,
    created_at TEXT NOT NULL,
    changed_by TEXT,
    changed_at TEXT,
    deleted_at TEXT,
    deleted_by TEXT
);

CREATE TABLE IF NOT EXISTS outage_window_sites (
    window_id  INTEGER NOT NULL,
    site_octet TEXT NOT NULL,
    PRIMARY KEY (window_id, site_octet)
);

CREATE INDEX IF NOT EXISTS idx_orion_events_node ON orion_events(node_id, event_id);
CREATE INDEX IF NOT EXISTS idx_orion_events_time ON orion_events(event_time);
CREATE INDEX IF NOT EXISTS idx_orion_nodes_device ON orion_nodes(device_id);
"""

TIERS = (0, 1, 2, 3, 4)
EVENT_TYPES = (op.EVENT_DOWN, op.EVENT_UP)


def ensure_tables(conn) -> None:
    """Create the outage tables and add the sites.tier column. Called by db.init_db()."""
    conn.executescript(SCHEMA)
    db._ensure_column(conn, "sites", "tier", "INTEGER")


def fmt_time(value) -> str:
    """A datetime as the text stored in the database."""
    return value.isoformat(sep=" ")


# ---------------------------------------------------------------------
# Site tiers
# ---------------------------------------------------------------------

def validate_tier(value):
    """Return the tier as an int 0-4, or None for "no tier". Raises ValueError for
    anything else. Call it BEFORE queueing a write: the queue retries a failing
    write on every drain."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"tier must be one of {list(TIERS)} (or empty for none), not {value!r}")
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ValueError(f"tier must be one of {list(TIERS)} (or empty for none), not {value!r}") from None
    if number not in TIERS:
        raise ValueError(f"tier must be one of {list(TIERS)} (or empty for none), not {value!r}")
    return number


def set_site_tier(conn, site_id: int, tier) -> bool:
    """Set (or clear) one site's tier. Returns True if it changed."""
    return set_site_tiers(conn, [site_id], tier) == 1


def set_site_tiers(conn, site_ids, tier) -> int:
    """Set (or clear) the tier of several sites in one step, logged once.
    Returns how many sites changed. Unknown or deleted sites raise ValueError
    before anything is changed."""
    tier = validate_tier(tier)
    ids = list(dict.fromkeys(int(i) for i in site_ids))
    if not ids:
        return 0
    rows = {}
    for site_id in ids:
        row = conn.execute("SELECT id, site_octet, tier FROM sites WHERE id = ? AND deleted_at IS NULL", (site_id,)).fetchone()
        if row is None:
            raise ValueError(f"site {site_id} does not exist (or was deleted)")
        rows[site_id] = row
    changed = [r for r in rows.values() if r["tier"] != tier]
    for row in changed:
        conn.execute("UPDATE sites SET tier = ? WHERE id = ?", (tier, row["id"]))
    if changed:
        label = "no tier" if tier is None else f"tier {tier}"
        if len(changed) == 1:
            db.log_activity("set_site_tier", f"Site {changed[0]['site_octet']} set to {label}", conn=conn)
        else:
            db.log_activity("set_site_tier", f"{len(changed)} sites set to {label}", conn=conn)
    return len(changed)


# ---------------------------------------------------------------------
# Storing a pull
# ---------------------------------------------------------------------

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_time(text, label):
    try:
        op.parse_time(text)
    except Exception:
        raise ValueError(f"{label} is not a usable time: {text!r}") from None


def validate_pull(nodes, events, pull) -> None:
    """Raise ValueError if the pull payload is malformed. Used before queueing."""
    if not isinstance(pull, dict):
        raise ValueError("pull must be a dict")
    for key in ("days", "events_fetched", "nodes_fetched", "nodes_matched"):
        if not _is_int(pull.get(key)) or pull[key] < 0:
            raise ValueError(f"pull[{key!r}] must be a non-negative integer, got {pull.get(key)!r}")
    if pull["days"] < 1:
        raise ValueError("pull['days'] must be at least 1")
    for key in ("server_now", "covered_from", "covered_to"):
        _check_time(pull.get(key), f"pull[{key!r}]")
    if pull.get("earliest_event") is not None:
        _check_time(pull["earliest_event"], "pull['earliest_event']")
    if op.parse_time(pull["covered_from"]) > op.parse_time(pull["covered_to"]):
        raise ValueError("pull covers a period that ends before it starts")
    seen_nodes = set()
    for n in nodes:
        if not _is_int(n.get("node_id")):
            raise ValueError(f"node without a numeric node_id: {n!r}")
        if n["node_id"] in seen_nodes:
            raise ValueError(f"node {n['node_id']} listed twice")
        seen_nodes.add(n["node_id"])
        for key in ("status", "device_id"):
            if n.get(key) is not None and not _is_int(n[key]):
                raise ValueError(f"node {n['node_id']}: {key} must be a number or empty, got {n[key]!r}")
    seen_events = set()
    for e in events:
        if not _is_int(e.get("event_id")) or not _is_int(e.get("node_id")):
            raise ValueError(f"event without numeric event_id and node_id: {e!r}")
        if e["event_id"] in seen_events:
            raise ValueError(f"event {e['event_id']} listed twice")
        seen_events.add(e["event_id"])
        if e.get("type") not in EVENT_TYPES:
            raise ValueError(f"event {e['event_id']}: type must be one of {EVENT_TYPES}, got {e.get('type')!r}")
        _check_time(e.get("time"), f"event {e['event_id']} time")


def store_orion_pull(conn, nodes, events, pull) -> dict:
    """Save one pull: upsert the nodes, add the events not already stored, and
    record the pull. Safe to repeat. Returns {"pull_id", "events_new", "events_seen", "nodes_new"}.

    nodes   [{"node_id", "caption", "ip", "status", "device_id", "match_method"}]
    events  [{"event_id", "node_id", "time" (text), "type"}]
    pull    {"server_now", "days", "covered_from", "covered_to", "earliest_event",
             "events_fetched", "nodes_fetched", "nodes_matched", "note"}
    """
    validate_pull(nodes, events, pull)
    now = db._now()
    nodes_new = 0
    for n in nodes:
        row = conn.execute("SELECT node_id FROM orion_nodes WHERE node_id = ?", (n["node_id"],)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO orion_nodes (node_id, caption, ip, status, device_id, match_method, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (n["node_id"], n.get("caption"), n.get("ip"), n.get("status"), n.get("device_id"), n.get("match_method"), now, now))
            nodes_new += 1
        elif n.get("device_id") is not None:
            conn.execute(
                "UPDATE orion_nodes SET caption = ?, ip = ?, status = ?, device_id = ?, match_method = ?, last_seen = ? WHERE node_id = ?",
                (n.get("caption"), n.get("ip"), n.get("status"), n["device_id"], n.get("match_method"), now, n["node_id"]))
        else:
            conn.execute(
                "UPDATE orion_nodes SET caption = ?, ip = ?, status = ?, last_seen = ? WHERE node_id = ?",
                (n.get("caption"), n.get("ip"), n.get("status"), now, n["node_id"]))
    events_new = 0
    for e in events:
        cur = conn.execute(
            "INSERT OR IGNORE INTO orion_events (event_id, node_id, event_time, event_type, pulled_at) VALUES (?, ?, ?, ?, ?)",
            (e["event_id"], e["node_id"], e["time"], e["type"], now))
        events_new += cur.rowcount if cur.rowcount > 0 else 0
    cur = conn.execute(
        "INSERT INTO orion_pulls (pulled_at, pulled_by, server_now, days, covered_from, covered_to, earliest_event, "
        "events_fetched, events_new, nodes_fetched, nodes_matched, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now, getattr(db, "CURRENT_USER", None), pull["server_now"], pull["days"], pull["covered_from"], pull["covered_to"],
         pull.get("earliest_event"), len(events), events_new, len(nodes), pull["nodes_matched"], pull.get("note")))
    db.log_activity("orion_pull", f"Orion pull: {events_new} new event(s) of {len(events)}, {pull['nodes_matched']} of {len(nodes)} node(s) matched", conn=conn)
    return {"pull_id": cur.lastrowid, "events_new": events_new, "events_seen": len(events), "nodes_new": nodes_new}


# ---------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------

def load_events(conn):
    """All stored Down/Up events as the dicts outage_calc expects, oldest EventID first."""
    return [{"event_id": r["event_id"], "node_id": r["node_id"], "time": op.parse_time(r["event_time"]), "type": r["event_type"]}
            for r in conn.execute("SELECT event_id, node_id, event_time, event_type FROM orion_events ORDER BY event_id")]


def load_matches(conn) -> dict:
    """Stored node-to-device matches in the shape orion_match.match_nodes() returns
    (the parts outage_calc uses). Only devices that are still live in SAD count."""
    matches, by_device = {}, {}
    rows = conn.execute(
        "SELECT n.node_id, n.device_id, n.match_method FROM orion_nodes n "
        "JOIN devices d ON d.id = n.device_id AND d.deleted_at IS NULL AND d.marked_stale_at IS NULL "
        "ORDER BY n.node_id")
    for r in rows:
        matches[r["node_id"]] = {"device_id": r["device_id"], "method": r["match_method"] or "stored"}
        by_device.setdefault(r["device_id"], []).append(r["node_id"])
    return {"matches": matches, "node_ids_by_device": by_device,
            "multi_node_devices": sorted(d for d, ids in by_device.items() if len(ids) > 1)}


def list_pulls(conn, limit: int = 20):
    """Most recent pulls first."""
    return conn.execute("SELECT * FROM orion_pulls ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()


def last_pull(conn):
    rows = list_pulls(conn, 1)
    return rows[0] if rows else None


def coverage(conn):
    """The periods the stored events cover, as a list of (start, end) datetimes
    (Orion server time), with overlapping pulls joined. More than one entry means
    a gap: nobody pulled for longer than Orion keeps events, so events in between
    are lost."""
    spans = sorted((op.parse_time(r["covered_from"]), op.parse_time(r["covered_to"]))
                   for r in conn.execute("SELECT covered_from, covered_to FROM orion_pulls"))
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def event_counts(conn) -> dict:
    """{"events", "down", "up", "nodes", "nodes_matched"} for what is stored."""
    one = lambda sql: conn.execute(sql).fetchone()[0]
    return {"events": one("SELECT COUNT(*) FROM orion_events"),
            "down": one(f"SELECT COUNT(*) FROM orion_events WHERE event_type = {op.EVENT_DOWN}"),
            "up": one(f"SELECT COUNT(*) FROM orion_events WHERE event_type = {op.EVENT_UP}"),
            "nodes": one("SELECT COUNT(*) FROM orion_nodes"),
            "nodes_matched": one("SELECT COUNT(*) FROM orion_nodes WHERE device_id IS NOT NULL")}


# ---------------------------------------------------------------------
# Planned downtime windows (times are Eastern wall-clock, like the events)
# ---------------------------------------------------------------------

WINDOW_KINDS = ("planned",)
WINDOW_SOURCES = ("manual", "from_outage")
MAX_WINDOW_DAYS = 366
MAX_REASON_CHARS = 300


def _window_time(value, label):
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    try:
        return op.parse_time(value).replace(microsecond=0)
    except Exception:
        raise ValueError(f"{label} is not a usable date and time: {value!r}") from None


def _clean_reason(reason):
    text = " ".join(str(reason or "").split())
    if len(text) > MAX_REASON_CHARS:
        raise ValueError(f"reason is {len(text)} characters; the limit is {MAX_REASON_CHARS}")
    return text or None


def validate_window(sites, start, end, reason=None):
    """Check a planned window and return (sites, start, end, reason) cleaned up:
    sites is None for "every site" or a sorted list of site octets (text), start and
    end are datetimes. Raises ValueError with a plain reason. Call it BEFORE queueing."""
    if sites is not None:
        if isinstance(sites, str):
            sites = [x for x in sites.replace(" ", "").split(",") if x]
        sites = sorted({str(x).strip() for x in sites if str(x).strip()})
        if not sites:
            raise ValueError("pick at least one site, or all sites")
    start, end = _window_time(start, "start"), _window_time(end, "end")
    if end <= start:
        raise ValueError(f"the window must end after it starts ({start} to {end})")
    if (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError(f"a window longer than {MAX_WINDOW_DAYS} days is probably a typo ({start} to {end})")
    return sites, start, end, _clean_reason(reason)


def _live_octets(conn):
    return {r["site_octet"] for r in conn.execute("SELECT site_octet FROM sites WHERE deleted_at IS NULL")}


def _window_row(conn, window_id):
    row = conn.execute("SELECT * FROM outage_windows WHERE id = ?", (int(window_id),)).fetchone()
    if row is None:
        raise ValueError(f"there is no planned window number {window_id}")
    return row


def _site_label(sites):
    return "all sites" if sites is None else "site " + ", ".join(sites)


def add_planned_window(conn, sites, start, end, reason=None, source="manual") -> int:
    """Add a planned downtime window. `sites` is None for every site or a list of
    site octets. Returns the new window's id."""
    sites, start, end, reason = validate_window(sites, start, end, reason)
    if source not in WINDOW_SOURCES:
        raise ValueError(f"source must be one of {WINDOW_SOURCES}")
    if sites is not None:
        live = _live_octets(conn)
        unknown = [x for x in sites if x not in live]
        if unknown:
            raise ValueError("no such site: " + ", ".join(unknown))
    cur = conn.execute(
        "INSERT INTO outage_windows (kind, all_sites, start_time, end_time, reason, source, created_by, created_at) "
        "VALUES ('planned', ?, ?, ?, ?, ?, ?, ?)",
        (1 if sites is None else 0, fmt_time(start), fmt_time(end), reason, source, getattr(db, "CURRENT_USER", None), db._now()))
    window_id = cur.lastrowid
    for octet in sites or []:
        conn.execute("INSERT INTO outage_window_sites (window_id, site_octet) VALUES (?, ?)", (window_id, octet))
    db.log_activity("add_planned_window", f"Planned window {window_id}: {_site_label(sites)}, {fmt_time(start)} to {fmt_time(end)}"
                    + (f" ({reason})" if reason else ""), conn=conn)
    return window_id


def update_planned_window(conn, window_id, start=None, end=None, reason=None) -> bool:
    """Change a window's start, end or reason (None leaves a value alone; an empty
    reason clears it). Used to trim a window. Returns True if anything changed."""
    row = _window_row(conn, window_id)
    if row["deleted_at"]:
        raise ValueError(f"planned window {window_id} is deleted; restore it first")
    new_start = _window_time(start, "start") if start is not None else op.parse_time(row["start_time"])
    new_end = _window_time(end, "end") if end is not None else op.parse_time(row["end_time"])
    new_reason = _clean_reason(reason) if reason is not None else row["reason"]
    _sites, new_start, new_end, _r = validate_window(["x"], new_start, new_end)
    if fmt_time(new_start) == row["start_time"] and fmt_time(new_end) == row["end_time"] and new_reason == row["reason"]:
        return False
    conn.execute("UPDATE outage_windows SET start_time = ?, end_time = ?, reason = ?, changed_by = ?, changed_at = ? WHERE id = ?",
                 (fmt_time(new_start), fmt_time(new_end), new_reason, getattr(db, "CURRENT_USER", None), db._now(), int(window_id)))
    db.log_activity("update_planned_window", f"Planned window {window_id} now {fmt_time(new_start)} to {fmt_time(new_end)}", conn=conn)
    return True


def delete_planned_window(conn, window_id) -> None:
    """Soft-delete a window. It stops counting everywhere and can be restored."""
    row = _window_row(conn, window_id)
    if row["deleted_at"]:
        raise ValueError(f"planned window {window_id} is already deleted")
    conn.execute("UPDATE outage_windows SET deleted_at = ?, deleted_by = ? WHERE id = ?",
                 (db._now(), getattr(db, "CURRENT_USER", None), int(window_id)))
    db.log_activity("delete_planned_window", f"Planned window {window_id} deleted", conn=conn)


def restore_planned_window(conn, window_id) -> None:
    row = _window_row(conn, window_id)
    if not row["deleted_at"]:
        raise ValueError(f"planned window {window_id} is not deleted")
    conn.execute("UPDATE outage_windows SET deleted_at = NULL, deleted_by = NULL WHERE id = ?", (int(window_id),))
    db.log_activity("restore_planned_window", f"Planned window {window_id} restored", conn=conn)


def load_planned_windows(conn, include_deleted: bool = False):
    """Planned windows as the dicts outage_calc expects (id, sites, start, end, deleted)
    plus reason, source, created_by, created_at. `sites` is None for all sites."""
    out = []
    sql = "SELECT * FROM outage_windows WHERE kind = 'planned'" + ("" if include_deleted else " AND deleted_at IS NULL") + " ORDER BY start_time, id"
    for r in conn.execute(sql):
        sites = None
        if not r["all_sites"]:
            sites = [x["site_octet"] for x in conn.execute(
                "SELECT site_octet FROM outage_window_sites WHERE window_id = ? ORDER BY site_octet", (r["id"],))]
        out.append({"id": r["id"], "sites": sites, "start": op.parse_time(r["start_time"]), "end": op.parse_time(r["end_time"]),
                    "deleted": bool(r["deleted_at"]), "reason": r["reason"], "source": r["source"],
                    "created_by": r["created_by"], "created_at": r["created_at"]})
    return out