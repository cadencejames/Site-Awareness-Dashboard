"""
db.py - SQLite database layer for the Site Awareness Dashboard (SAD).

This module owns the schema and provides small, dependency-free helper
functions for reading and writing discovery data. Nothing outside this
file should be writing raw SQL against sad.db - everything goes through
these functions so the schema can evolve without hunting through the
whole codebase.

Design notes (decided during planning, not obvious from the code alone):
- Every discovery-derived table carries `source` and `last_seen` so we
  always know *where* a row came from and *how fresh* it is.
- `raw_source_data` stores the original parsed line/record as text so
  we can reprocess history later if parsing logic changes, without a
  full network re-scan.
- Sources in use: 'cdp' (CDP walk discovery) and 'inventory_sync'
  (bulk import from whatever inventory tool is current - Prime,
  Catalyst Center, etc; deliberately not tool-specific since the tool
  itself is expected to change over time). 'manual' is reserved for
  a person explicitly overriding a value by hand.
- devices.mgmt_ip is a CACHED "best known IP" value, derived from
  device_ips (see below) - nothing should write to it directly other
  than _refresh_cached_mgmt_ip(). It exists as a fast/simple column
  for anything that just wants "the IP to use right now" without
  caring about ranking logic.
- device_ips holds every IP ever observed for a device, tagged with
  its own source, and is never destructively overwritten - only
  added to. This exists because CDP-reported "management addresses"
  are sometimes wrong (e.g. a router advertising an unroutable
  interface-local IP instead of its real mgmt IP), so blindly trusting
  the most recent report isn't safe. inventory_sync data
  (the org's actual system of record) always outranks cdp data when
  picking the current best IP; among same-source candidates, an IP
  whose first two octets match another known-good IP at the same site
  is preferred as a tie-breaker. See record_device_ip() and
  get_best_ip_for_device().
"""

import sqlite3
import ipaddress
import mac_parser
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = "sad.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    site_octet         TEXT NOT NULL UNIQUE,
    site_name          TEXT UNIQUE,
    site_code          TEXT,
    seed_device        TEXT,
    arp_seed_device    TEXT,  -- only set when it differs from seed_device - see get_arp_seed_device_for_site()
    last_run           TEXT,  -- legacy, frozen - see last_cdp_discovery
    last_cdp_discovery TEXT,  -- set only by mark_site_run(); the trustworthy "was this site actually scanned" signal
    last_arp_collection TEXT,  -- set only by mark_site_arp_run(); same idea as last_cdp_discovery, for ARP collection
    last_mac_table_collection TEXT  -- set only by mark_site_mac_table_run(); same idea again, for MAC-table/client collection specifically. NOT the same as last_cdp_discovery, which advances on every CDP walk regardless of whether MAC-table collection was actually requested that run - client staleness needs its own timestamp to avoid every client looking newly stale after a plain CDP-only re-scan
);

CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id         INTEGER NOT NULL REFERENCES sites(id),
    hostname        TEXT NOT NULL,
    mgmt_ip         TEXT,
    platform        TEXT,
    serial_number   TEXT,
    is_seed         INTEGER NOT NULL DEFAULT 0,
    is_arp_seed     INTEGER NOT NULL DEFAULT 0,
    arp_override_ip TEXT,  -- tried first for ARP collection only (e.g. a shared VIP); falls through to the device's normal ranked IPs if unreachable
    marked_stale_at TEXT,  -- set only by set_device_marked_stale(); a manual "I've confirmed this is gone" flag, independent of and complementary to the automatic age-based staleness check
    source          TEXT NOT NULL DEFAULT 'cdp',
    last_seen       TEXT,
    raw_source_data TEXT,
    UNIQUE(site_id, hostname)
);

CREATE TABLE IF NOT EXISTS links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id         INTEGER NOT NULL REFERENCES sites(id),
    device_a_id     INTEGER NOT NULL REFERENCES devices(id),
    device_b_id     INTEGER NOT NULL REFERENCES devices(id),
    local_intf      TEXT,
    remote_intf     TEXT,
    source          TEXT NOT NULL DEFAULT 'cdp',
    last_seen       TEXT,
    raw_source_data TEXT,
    UNIQUE(site_id, device_a_id, device_b_id, local_intf, remote_intf)
);

CREATE TABLE IF NOT EXISTS arp_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id         INTEGER NOT NULL REFERENCES sites(id),
    device_id       INTEGER REFERENCES devices(id),
    vrf             TEXT NOT NULL DEFAULT '',
    ip              TEXT NOT NULL,
    mac             TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'cdp',
    last_seen       TEXT,
    raw_source_data TEXT,
    UNIQUE(site_id, vrf, ip, mac)
);

-- One row per MAC per site - the switch/port it was last confirmed
-- plugged into (a real edge port, not an inter-switch uplink; see
-- mac_parser.py and the correlation pass that decides that before
-- ever calling upsert_client()). IP isn't stored here - it's
-- correlated against arp_entries.mac at display time instead of
-- duplicated, so the two facts can't drift out of sync with each
-- other. The UNIQUE constraint is a plain-string backstop; the real
-- duplicate-prevention happens in upsert_client() itself via
-- mac_parser.normalize_mac(), since the SAME physical MAC can be
-- reported in different separator styles/case across platforms and
-- commands - a raw string UNIQUE constraint alone wouldn't catch that.
CREATE TABLE IF NOT EXISTS clients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id         INTEGER NOT NULL REFERENCES sites(id),
    mac             TEXT NOT NULL,
    device_id       INTEGER REFERENCES devices(id),
    interface       TEXT,
    vlan            TEXT,
    device_type     TEXT,
    source          TEXT NOT NULL DEFAULT 'mac-table',
    last_seen       TEXT,
    raw_source_data TEXT,
    UNIQUE(site_id, mac)
);

-- Raw "show mac address-table" output, staged per device during a CDP
-- walk (see discover_site()'s collect_mac_tables option), NOT yet
-- correlated into clients. Deciding which entries are real edge/
-- client ports versus inter-switch uplinks requires the site's full
-- links table to be complete, which it isn't yet mid-walk - that
-- correlation is a separate pass, run only after the walk finishes.
-- One row per device (not accumulating history) - a later collection
-- for the same device REPLACES its previous staged output, matching
-- the same "last processed wins" philosophy used throughout.
CREATE TABLE IF NOT EXISTS mac_table_raw (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id      INTEGER NOT NULL REFERENCES sites(id),
    device_id    INTEGER NOT NULL REFERENCES devices(id),
    raw_output   TEXT NOT NULL,
    collected_at TEXT,
    UNIQUE(device_id)
);

-- CUCM/RIS enrichment for phone/VTC clients (see clients.device_type
-- 'cisco_phone'/'vtc'). Keyed by MAC alone, no site_id - stays valid
-- even if a phone physically moves between sites, since it's joined
-- against whichever site clients.mac currently belongs to, not tied
-- to one. mac is stored NORMALIZED (lowercase, no separators) here
-- specifically - unlike clients.mac/arp_entries.mac, which preserve
-- whatever a network device reported verbatim, this table's mac is
-- SAD's own generated join key (built to construct the SEPxxxx CUCM
-- lookup name), not text captured from an external source.
--
-- Two genuinely different refresh rhythms live on this one row:
-- ris_ip/ris_status come from RIS's cheap bulk query and are meant to
-- be refreshed on EVERY enrichment script run (ris_status in
-- particular can genuinely flip between Registered/Unregistered over
-- time) - no skip logic for those two fields at all. model/
-- serial_number come from a slow, per-device HTTP/xAPI call instead,
-- and are governed by last_attempted/last_enriched: last_attempted is
-- stamped on every attempt regardless of outcome (so a permanently-
-- unreachable legacy device is only ever tried once, not retried
-- forever), while last_enriched is stamped only when the pull
-- actually returned something real.
-- Activity log: SAD's operational record of who did what and when.
-- Every part of the app that logs anything does so through
-- db.log_activity() ONLY - nothing writes to this table directly.
-- That centralization is deliberate: it's what will let a future
-- syslog-forwarding feature be a small addition inside that one
-- function later, rather than needing to touch every call site again.
-- NOT tamper-proof (a local sqlite table like any other, editable by
-- anyone with file access to sad.db) - a genuinely useful operational
-- record, not a true audit trail, unless/until forwarded elsewhere.
-- credential_store is nullable - only set for actions that actually
-- involved unlocking one; detail_ref is nullable - a pointer to a
-- fuller file (e.g. one of Command Runner's own per-run logs) when
-- one exists for this entry.
CREATE TABLE IF NOT EXISTS activity_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT NOT NULL,
    username         TEXT,
    credential_store TEXT,
    action           TEXT NOT NULL,
    detail           TEXT,
    detail_ref       TEXT
);

CREATE TABLE IF NOT EXISTS phone_enrichment (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    mac            TEXT NOT NULL UNIQUE,
    device_name    TEXT,
    ris_ip         TEXT,
    ris_status     TEXT,
    phone_number   TEXT,  -- from RIS's DirNumber field (same bulk call as ris_ip/ris_status - refreshed every run, no skip logic, same reasoning as ris_status since an extension can genuinely be reassigned over time)
    model          TEXT,
    serial_number  TEXT,
    last_attempted TEXT,
    last_enriched  TEXT
);

CREATE TABLE IF NOT EXISTS subnet_overrides (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    cidr     TEXT NOT NULL UNIQUE,
    site_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_ips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER NOT NULL REFERENCES devices(id),
    ip              TEXT NOT NULL,
    source          TEXT NOT NULL,
    last_seen       TEXT,
    raw_source_data TEXT,
    UNIQUE(device_id, ip)
);

CREATE INDEX IF NOT EXISTS idx_devices_site   ON devices(site_id);
CREATE INDEX IF NOT EXISTS idx_devices_serial ON devices(serial_number);
CREATE INDEX IF NOT EXISTS idx_links_site     ON links(site_id);
CREATE INDEX IF NOT EXISTS idx_arp_site       ON arp_entries(site_id);
CREATE INDEX IF NOT EXISTS idx_device_ips_device ON device_ips(device_id);
"""


def _now() -> str:
    """UTC ISO-8601 timestamp used for every last_seen/last_run write."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------
# Activity logging
# ---------------------------------------------------------------------
# Set once by the login gate at launch (see gui.py) after a successful
# unlock - not recomputed per log call, since os.getlogin() can raise
# in some execution contexts (no controlling terminal, run as a
# service) and the login flow is a known-good moment to capture it
# safely. CURRENT_CREDENTIAL_STORE is which credential store file got
# unlocked; both stay None until login happens, and log_activity()
# logs whatever they currently are, blank or not - it has no opinion
# about whether login has happened yet, that's the login gate's job.
CURRENT_USER = None
CURRENT_CREDENTIAL_STORE = None


def log_activity(action: str, detail: str = "", detail_ref: str = None, conn=None) -> None:
    """THE single write path for SAD's activity log - every part of
    the app that wants to record something calls this, and ONLY this;
    nothing should ever INSERT INTO activity_log directly. This
    centralization is deliberate: it's what lets a future syslog-
    forwarding feature be a small addition inside this one function
    later, rather than needing to touch every call site across the
    app again.

    conn: an already-open connection to reuse, if the caller has one
    (e.g. db.py's own write functions, called from within a caller's
    still-open, uncommitted transaction). This matters for a concrete
    reason, not just convenience: opening a SEPARATE connection while
    another one is mid-transaction on the same SQLite file can hit
    "database is locked" - confirmed directly, not theoretical. Reusing
    the caller's connection avoids that entirely, since it's the same
    connection/transaction, not two competing ones. If no conn is
    given (e.g. the login dialog, which has no open transaction to
    reuse), falls back to opening its own independent one.

    Strictly best-effort either way: catches and swallows its own
    failures (a locked db, a full disk, whatever) rather than ever
    raising - logging must never be able to block or break the action
    it's attached to. A failure here prints a quiet warning and
    otherwise does nothing further.
    """
    try:
        if conn is not None:
            conn.execute(
                """
                INSERT INTO activity_log (timestamp, username, credential_store, action, detail, detail_ref)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_now(), CURRENT_USER, CURRENT_CREDENTIAL_STORE, action, detail, detail_ref),
            )
        else:
            with get_conn() as own_conn:
                own_conn.execute(
                    """
                    INSERT INTO activity_log (timestamp, username, credential_store, action, detail, detail_ref)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (_now(), CURRENT_USER, CURRENT_CREDENTIAL_STORE, action, detail, detail_ref),
                )
    except Exception as e:
        print(f"Warning: could not write activity log entry: {e}")


@contextmanager
def get_conn(db_path: str = DB_PATH):
    """Context-managed connection with foreign keys enforced."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn, table: str, column: str, coltype: str) -> None:
    """Adds `column` to `table` if it isn't already there. Necessary
    because CREATE TABLE IF NOT EXISTS (used by init_db() below) is a
    complete no-op on a table that already exists - a new column
    added to SCHEMA's CREATE TABLE text does NOT retroactively apply
    to a database that was created before that column was added
    (verified directly: SQLite skips the whole statement once the
    table is present, new column or not). Every column added to an
    already-existing table needs an entry here, or it silently never
    reaches anyone's real, pre-existing database - it would only ever
    appear on a brand new one, and any code touching the missing
    column would hit a hard "no such column" SQL error there instead.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db(db_path: str = DB_PATH) -> None:
    """Create all tables/indexes if they don't already exist, and
    migrate an existing database forward to match the current schema
    (see _ensure_column()'s docstring for why this step is needed at
    all - a plain CREATE TABLE IF NOT EXISTS cannot add new columns to
    a table that's already there).
    """
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "sites", "last_mac_table_collection", "TEXT")
        _ensure_column(conn, "phone_enrichment", "phone_number", "TEXT")
        # Soft-delete support: NULL = active, a real timestamp = hidden
        # from every listing/lookup below but still physically present
        # (reversible by design - see soft_delete_site()/
        # soft_delete_device()). No equivalent flag on links/arp_
        # entries/clients - their hiding is entirely derived from
        # whichever devices make it through the filtered queries below.
        _ensure_column(conn, "sites", "deleted_at", "TEXT")
        _ensure_column(conn, "devices", "deleted_at", "TEXT")
        # Retroactive coverage for a column added to an already-
        # existing table in an earlier session - included here in
        # case any real database predates it and was never recreated
        # from scratch since (this call is a safe no-op otherwise).
        _ensure_column(conn, "devices", "marked_stale_at", "TEXT")


# ---------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------

def upsert_site(conn, site_octet: str) -> int:
    """Insert a site by octet if new. Returns site_id.

    Does NOT touch last_run or last_cdp_discovery - those are
    exclusively set by mark_site_run() when a CDP discovery run
    actually completes for a site. Earlier versions of this function
    also stamped last_run here, which meant every inventory import
    call (not just real discovery runs) bumped it - making it
    impossible to tell which sites had genuinely been CDP-scanned.
    See last_cdp_discovery for the trustworthy signal going forward;
    last_run is left alone as a frozen legacy field, not actively used.

    site_name and site_code are NOT set here - discovery/import only
    ever knows the octet. Use set_site_identity() to assign the
    human-friendly name and org code afterward.
    """
    conn.execute(
        "INSERT INTO sites (site_octet) VALUES (?) ON CONFLICT(site_octet) DO NOTHING",
        (site_octet,),
    )
    row = conn.execute("SELECT id FROM sites WHERE site_octet = ?", (site_octet,)).fetchone()
    return row["id"]


def set_site_identity(conn, site_id: int, site_name: str = None, site_code: str = None) -> None:
    """Manually assign the human-friendly name and/or org code for a site.

    Pass only the field(s) you want to change - the other stays as-is.
    """
    if site_name is not None:
        conn.execute("UPDATE sites SET site_name = ? WHERE id = ?", (site_name, site_id))
    if site_code is not None:
        conn.execute("UPDATE sites SET site_code = ? WHERE id = ?", (site_code, site_id))


def add_site_manual(conn, site_octet: str, site_name: str = None, site_code: str = None) -> int:
    """User-initiated "Add Site" (Site Manager GUI). Thin wrapper around
    upsert_site() + set_site_identity() that additionally logs the
    action - deliberately NOT folded into upsert_site() itself, since
    upsert_site() is also called on every ordinary CDP/inventory
    discovery pass, where logging every call would flood the activity
    log with routine machinery instead of the one thing an operator
    actually cares about seeing: "a person added this site by hand".
    If the octet already exists, this reuses that row rather than
    erroring - upsert_site()'s normal behavior. If that existing row
    was soft-deleted, re-adding it by the same octet is treated as an
    intentional revival: deleted_at is cleared here so the site comes
    back to life, rather than the user's "Add Site" silently doing
    nothing because a same-named row already exists but is hidden.
    """
    site_id = upsert_site(conn, site_octet)
    conn.execute("UPDATE sites SET deleted_at = NULL WHERE id = ?", (site_id,))
    set_site_identity(conn, site_id, site_name=site_name, site_code=site_code)
    label = site_name or site_octet
    log_activity("add_site", f"Site {site_octet} ({label}) added", conn=conn)
    return site_id


def get_all_sites(conn, include_unassigned: bool = True):
    # site_octet is stored as TEXT (it isn't always a literal octet -
    # subnet-override keys like "170a" or "unassigned" live there too),
    # so a plain ORDER BY site_octet sorts alphabetically ("100" before
    # "20"), not numerically. CAST(...AS INTEGER) fixes the numeric
    # case; non-numeric octets all CAST to 0 in SQLite, so they group
    # together at the front, sorted alphabetically among themselves via
    # the site_octet/site_name tiebreakers - which is also the only
    # place site_name ever actually breaks a tie, since site_octet
    # itself is UNIQUE.
    #
    # include_unassigned=False (opt-in) excludes the reserved
    # "Unassigned" holding site (see get_or_create_unassigned_site()) -
    # it never has a seed device (nothing ever flags one for it), so a
    # discovery/ARP run against it is pure overhead: it's picked up,
    # immediately reports "no seed device flagged", and moves on. Every
    # caller that means "every real, scannable site" should pass this;
    # callers that manage or report on Unassigned's actual (just
    # misattributed, not fake) devices - Site Manager, Command Runner's
    # device scope, CSV export - keep the default and still see it.
    where = "WHERE deleted_at IS NULL"
    params = ()
    if not include_unassigned:
        where += " AND site_octet != ?"
        params = (UNASSIGNED_SITE_OCTET,)
    return conn.execute(
        f"SELECT * FROM sites {where} ORDER BY CAST(site_octet AS INTEGER), site_octet, site_name",
        params,
    ).fetchall()


def find_site_by_any_key(conn, key: str):
    """Look up a site by site_octet, site_name, or site_code - checked
    in that strict priority order, not all at once. site_octet is
    guaranteed unique, so an exact octet match is always authoritative
    and returned immediately; only falls through to name, then code,
    if no octet matches. This matters because site_code is
    intentionally NOT unique (real sites can share one) - a single
    combined OR query with no priority could return a coincidental
    name/code match instead of the real octet match, silently
    resolving to the wrong site. Case-insensitive on name/code since
    those are typed by hand more often than octet. Returns the site
    row, or None if nothing matches any of the three.
    """
    row = conn.execute("SELECT * FROM sites WHERE site_octet = ? AND deleted_at IS NULL", (key,)).fetchone()
    if row is not None:
        return row
    row = conn.execute(
        "SELECT * FROM sites WHERE LOWER(site_name) = LOWER(?) AND deleted_at IS NULL", (key,)
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT * FROM sites WHERE LOWER(site_code) = LOWER(?) AND deleted_at IS NULL LIMIT 1", (key,)
    ).fetchone()


def get_site_by_id(conn, site_id: int):
    """Pure lookup by primary key - unambiguous by definition, unlike
    any string-based lookup. Used where a caller already has a
    specific site's id in hand (e.g. from a GUI selection) and wants
    to re-fetch it without any risk of a name/code/octet collision.
    Returns None for a soft-deleted site, same as if it didn't exist -
    a caller that genuinely needs to see a deleted site (e.g. a future
    restore tool) should query sites directly rather than through this.
    """
    return conn.execute("SELECT * FROM sites WHERE id = ? AND deleted_at IS NULL", (site_id,)).fetchone()


def get_site_by_octet(conn, site_octet: str):
    """Pure lookup by exact site_octet - a plain SELECT with no side
    effects. Unlike upsert_site(), this never creates a row and never
    touches last_run. Use this when you need to check whether a site
    already exists without implying "a run just happened" (e.g.
    resolving which site a CDP neighbor actually belongs to, where the
    site itself wasn't what was being scanned). Returns None if no
    site has that octet.
    """
    return conn.execute(
        "SELECT * FROM sites WHERE site_octet = ?", (site_octet,)
    ).fetchone()


def find_devices_by_hostname_anywhere(conn, hostname: str):
    """All devices with this exact hostname across every site (should
    normally be 0 or 1, given hostnames are expected to be unique org-
    wide - but this doesn't enforce that, so more than one is possible
    and callers should treat that as ambiguous rather than picking
    one). Each row includes the owning site's site_octet as
    owning_site_octet, for convenience in messages/logging.
    """
    return conn.execute(
        """
        SELECT d.*, s.site_octet AS owning_site_octet
        FROM devices d
        JOIN sites s ON d.site_id = s.id
        WHERE d.hostname = ?
        """,
        (hostname,),
    ).fetchall()


UNASSIGNED_SITE_OCTET = "unassigned"


def get_or_create_unassigned_site(conn) -> int:
    """Returns the id of the reserved 'Unassigned' site, creating it
    (with a human-readable name) the first time it's needed.

    This is where a CDP-discovered neighbor lands when it can't be
    confidently attributed to any real site: its address's octet
    doesn't match the site being scanned (e.g. it's a WAN-facing
    address, which doesn't follow the normal per-site addressing
    scheme at all), AND its hostname doesn't match any device already
    known elsewhere. Rather than guess wrong, the device/link still
    get recorded - nothing is silently dropped - just parked here
    pending a person reviewing site_manager.py and reassigning it once
    its real site is known.
    """
    existing = get_site_by_octet(conn, UNASSIGNED_SITE_OCTET)
    if existing is not None:
        return existing["id"]
    site_id = upsert_site(conn, UNASSIGNED_SITE_OCTET)
    set_site_identity(conn, site_id, site_name="Unassigned - Needs Review")
    return site_id


# ---------------------------------------------------------------------
# Subnet overrides (for sites that share a second octet but are split
# by a non-/24 subnet boundary)
# ---------------------------------------------------------------------

def upsert_subnet_override(conn, cidr: str, site_key: str) -> None:
    """Add or update an exception mapping a specific CIDR range to a site key.

    site_key is just whatever unique string you want that site to be
    known by internally (it becomes that site's site_octet value) -
    it doesn't have to literally be an octet, e.g. '170a'/'170-guest'.
    """
    conn.execute(
        """
        INSERT INTO subnet_overrides (cidr, site_key)
        VALUES (?, ?)
        ON CONFLICT(cidr) DO UPDATE SET site_key = excluded.site_key
        """,
        (cidr, site_key),
    )


def get_all_subnet_overrides(conn):
    return conn.execute("SELECT * FROM subnet_overrides ORDER BY cidr").fetchall()


def resolve_site_key_for_ip(conn, ip_str: str) -> str:
    """Return the site key to use for a device's IP.

    Checks subnet_overrides first - if the IP falls in more than one
    override range (shouldn't normally happen, but be defensive), the
    most specific (longest prefix) match wins. Falls back to the plain
    second-octet rule if no override matches.
    """
    ip = ipaddress.ip_address(ip_str)

    overrides = conn.execute("SELECT cidr, site_key FROM subnet_overrides").fetchall()
    matches = []
    for row in overrides:
        network = ipaddress.ip_network(row["cidr"], strict=False)
        if ip in network:
            matches.append((network.prefixlen, row["site_key"]))

    if matches:
        matches.sort(key=lambda m: m[0], reverse=True)  # longest prefix first
        return matches[0][1]

    return str(ip).split(".")[1]


def mark_site_run(conn, site_id: int) -> None:
    """Stamp a site's last_cdp_discovery to now (call at the end of a
    successful discover_site() run) - the only thing that writes to
    this column, so it's a trustworthy "was this site genuinely
    CDP-scanned" signal. sites.last_run is a separate, legacy column
    that upsert_site() still bumps on every unrelated call (inventory
    imports, site creation, etc.) - it doesn't mean the same thing as
    last_cdp_discovery and shouldn't be used to judge scan freshness.
    """
    conn.execute("UPDATE sites SET last_cdp_discovery = ? WHERE id = ?", (_now(), site_id))


def mark_site_arp_run(conn, site_id: int) -> None:
    """Stamp a site's last_arp_collection to now (call at the end of a
    successful collect_arp() run) - same idea and same reasoning as
    mark_site_run()/last_cdp_discovery, just for ARP collection instead
    of CDP discovery. Nothing else should write to this column.
    """
    conn.execute("UPDATE sites SET last_arp_collection = ? WHERE id = ?", (_now(), site_id))


def mark_site_mac_table_run(conn, site_id: int) -> None:
    """Stamp a site's last_mac_table_collection to now (call at the
    end of a discover_site() run that had collect_mac_tables=True) -
    same idea as mark_site_run()/mark_site_arp_run(), but deliberately
    its own separate column rather than reusing last_cdp_discovery.
    last_cdp_discovery advances on EVERY CDP walk regardless of
    whether MAC-table collection was actually requested that run -
    comparing client staleness against it would make every client at
    a site look newly stale after a plain CDP-only re-scan, even
    though nothing about them was actually re-verified that time.
    """
    conn.execute("UPDATE sites SET last_mac_table_collection = ? WHERE id = ?", (_now(), site_id))


# ---------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------

def upsert_device(
    conn,
    site_id: int,
    hostname: str,
    mgmt_ip: str = None,
    platform: str = None,
    serial_number: str = None,
    source: str = "cdp",
    raw_source_data: str = None,
) -> int:
    """Insert or refresh a device seen at a site. Returns device_id.

    serial_number is optional in V1 - plain CDP neighbor data doesn't
    carry it. It's here so a later enrichment pass (show inventory/SNMP)
    can populate it without a schema change, since it's the one field
    that survives a hostname rename or re-IP.

    Rank-aware like record_device_ip(): if this device already exists
    from a higher-priority source (e.g. inventory_sync) and this call
    is a lower-priority one (e.g. cdp re-discovering it), source/
    platform/serial_number are NOT downgraded - only last_seen
    refreshes. Without this, a CDP walk re-discovering an
    inventory-known device would silently overwrite trusted data with
    whatever CDP happens to report about it.
    """
    existing = conn.execute(
        "SELECT source FROM devices WHERE site_id = ? AND hostname = ?", (site_id, hostname)
    ).fetchone()

    if existing is not None and _source_rank(source) < _source_rank(existing["source"]):
        conn.execute(
            "UPDATE devices SET last_seen = ? WHERE site_id = ? AND hostname = ?",
            (_now(), site_id, hostname),
        )
    else:
        conn.execute(
            """
            INSERT INTO devices (site_id, hostname, mgmt_ip, platform, serial_number, source, last_seen, raw_source_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site_id, hostname) DO UPDATE SET
                mgmt_ip         = excluded.mgmt_ip,
                platform        = excluded.platform,
                serial_number   = excluded.serial_number,
                source          = excluded.source,
                last_seen       = excluded.last_seen,
                raw_source_data = excluded.raw_source_data
            """,
            (site_id, hostname, mgmt_ip, platform, serial_number, source, _now(), raw_source_data),
        )

    row = conn.execute(
        "SELECT id FROM devices WHERE site_id = ? AND hostname = ?",
        (site_id, hostname),
    ).fetchone()
    return row["id"]


def get_devices_for_site(conn, site_id: int):
    return conn.execute(
        "SELECT * FROM devices WHERE site_id = ? AND deleted_at IS NULL ORDER BY hostname", (site_id,)
    ).fetchall()


def add_device_manual(
    conn, site_id: int, hostname: str, mgmt_ip: str = None, platform: str = None
) -> int:
    """User-initiated "Add Device" (Site Manager GUI). Thin wrapper
    around upsert_device() + logging, same reasoning as
    add_site_manual(): upsert_device() itself is called on every
    ordinary CDP/inventory pass and must stay silent, so the log entry
    lives here instead, on the one path that represents a person
    typing a device in by hand.

    source="manual" is fixed here (not a parameter) - manually-added
    devices sit at the bottom of upsert_device()'s source-priority
    ranking (see _source_rank()), so a later real CDP or inventory
    discovery of the same hostname correctly supersedes this row's
    platform/serial/source instead of a hand-typed placeholder
    permanently blocking real data from ever winning.

    If this device was previously soft-deleted, re-adding it by the
    same hostname at the same site revives it (deleted_at cleared),
    matching add_site_manual()'s revival behavior.
    """
    device_id = upsert_device(conn, site_id, hostname, mgmt_ip=mgmt_ip, platform=platform, source="manual")
    conn.execute("UPDATE devices SET deleted_at = NULL WHERE id = ?", (device_id,))
    log_activity(
        "add_device", f"{hostname} ({mgmt_ip or 'no IP'}) added to site id {site_id}", conn=conn
    )
    return device_id


def _soft_delete_device_no_log(conn, device_id: int):
    """Core soft-delete logic for ONE device, without its own logging
    call - shared by soft_delete_device() (a single-device delete,
    logs once) and soft_delete_site() (a whole-site delete that
    cascades to every device under it, but should still log ONCE for
    the site action, not once per device - matching the same "log the
    user's intent, not the underlying machinery" principle used
    everywhere else in the app). If this device is currently flagged
    as its site's CDP seed or ARP seed, that reference is cleared
    automatically (the site just reverts to "no seed set") rather than
    leaving a hidden device still referenced by a site that would
    otherwise try to use it for discovery. Returns the device's row as
    it was just before deletion, or None if it didn't exist / was
    already deleted.
    """
    row = conn.execute(
        "SELECT site_id, hostname, mgmt_ip, is_seed, is_arp_seed FROM devices WHERE id = ? AND deleted_at IS NULL",
        (device_id,),
    ).fetchone()
    if row is None:
        return None

    conn.execute("UPDATE devices SET deleted_at = ? WHERE id = ?", (_now(), device_id))
    if row["is_seed"]:
        conn.execute("UPDATE sites SET seed_device = NULL WHERE id = ?", (row["site_id"],))
    if row["is_arp_seed"]:
        conn.execute("UPDATE sites SET arp_seed_device = NULL WHERE id = ?", (row["site_id"],))
    return dict(row)


def soft_delete_device(conn, device_id: int) -> None:
    """Hides a device from every listing/lookup in the app without
    deleting its row or any history attached to it (links, ARP,
    clients, phone enrichment) - reversible by design, see the
    deleted_at columns' schema comment. A future helper script/
    function can restore or permanently purge soft-deleted rows; no
    such tool exists yet, on purpose - this is a one-way hide for now.
    """
    row = _soft_delete_device_no_log(conn, device_id)
    if row is None:
        return
    log_activity("delete_device", f"{row['hostname']} ({row['mgmt_ip'] or 'no IP'}) soft-deleted", conn=conn)


def get_device_delete_impact(conn, device_id: int) -> dict:
    """Counts of what references this device, for a delete
    confirmation - counts only, not full detail (the device itself is
    identified by name/IP in the confirmation UI; links/clients are
    just "how much is riding on this", not something worth listing
    individually).
    """
    links = conn.execute(
        "SELECT COUNT(*) AS c FROM links WHERE device_a_id = ? OR device_b_id = ?", (device_id, device_id)
    ).fetchone()["c"]
    clients = conn.execute(
        "SELECT COUNT(*) AS c FROM clients WHERE device_id = ?", (device_id,)
    ).fetchone()["c"]
    return {"links": links, "clients": clients}


def get_site_delete_impact(conn, site_id: int) -> dict:
    """Device list (hostname + mgmt_ip) plus aggregate link/client
    counts for a site-delete confirmation - devices are listed
    individually since their identity matters, links/clients are just
    counted since only the scale of what's affected matters there.
    """
    devices = conn.execute(
        "SELECT id, hostname, mgmt_ip FROM devices WHERE site_id = ? AND deleted_at IS NULL ORDER BY hostname",
        (site_id,),
    ).fetchall()
    device_ids = [d["id"] for d in devices]
    if device_ids:
        placeholders = ",".join("?" * len(device_ids))
        links = conn.execute(
            f"SELECT COUNT(*) AS c FROM links WHERE device_a_id IN ({placeholders}) OR device_b_id IN ({placeholders})",
            device_ids + device_ids,
        ).fetchone()["c"]
        clients = conn.execute(
            f"SELECT COUNT(*) AS c FROM clients WHERE device_id IN ({placeholders})", device_ids,
        ).fetchone()["c"]
    else:
        links = 0
        clients = 0
    return {"devices": [dict(d) for d in devices], "links": links, "clients": clients}


def soft_delete_site(conn, site_id: int) -> None:
    """Hides a site AND every device under it - reversible by design,
    same as soft_delete_device(). Logs once for the whole action
    regardless of how many devices it cascades to.
    """
    site_row = conn.execute(
        "SELECT site_octet FROM sites WHERE id = ? AND deleted_at IS NULL", (site_id,)
    ).fetchone()
    if site_row is None:
        return

    device_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM devices WHERE site_id = ? AND deleted_at IS NULL", (site_id,)
        ).fetchall()
    ]
    for device_id in device_ids:
        _soft_delete_device_no_log(conn, device_id)

    conn.execute("UPDATE sites SET deleted_at = ? WHERE id = ?", (_now(), site_id))

    log_activity(
        "delete_site", f"Site {site_row['site_octet']} and {len(device_ids)} device(s) soft-deleted", conn=conn,
    )


def get_device_id_by_hostname(conn, site_id: int, hostname: str):
    """Look up a device's id by hostname without touching its row - used
    when you already know a device exists (e.g. the seed, or a device
    discovered earlier in the same walk) and just need its id, without
    risking an upsert overwriting fields you don't have fresh data for.
    Returns None if not found.
    """
    row = conn.execute(
        "SELECT id FROM devices WHERE site_id = ? AND hostname = ?", (site_id, hostname)
    ).fetchone()
    return row["id"] if row else None


def get_device_by_hostname(conn, site_id: int, hostname: str):
    """Same lookup as get_device_id_by_hostname, but returns the full
    row instead of just the id - used when a caller needs more than
    the id (e.g. the orchestrator's platform-substring connection
    fast-path, which needs the device's known platform too).
    """
    return conn.execute(
        "SELECT * FROM devices WHERE site_id = ? AND hostname = ?", (site_id, hostname)
    ).fetchone()


def update_device_mgmt_ip(conn, device_id: int, mgmt_ip: str) -> None:
    """Narrow update: refresh just mgmt_ip and last_seen, without
    touching any other field. Used when the actually-reachable IP
    differs from what CDP originally reported for a device - e.g. a
    connection fallback to entry_ip succeeded after the reported
    mgmt_ip failed.
    """
    conn.execute("UPDATE devices SET mgmt_ip = ?, last_seen = ? WHERE id = ?", (mgmt_ip, _now(), device_id))


def set_device_platform(conn, device_id: int, platform: str, source: str = "manual") -> None:
    """Narrow update: set just platform (and source/last_seen), without
    touching mgmt_ip/serial_number/raw_source_data. Used for manually
    correcting known-bad platform data from an inventory tool (e.g.
    "Unsupported Cisco Device"). Defaults source to 'manual' - the
    highest rank in _SOURCE_RANK - so the correction is protected from
    being silently overwritten by a future same-or-lower-rank re-sync.
    """
    conn.execute("UPDATE devices SET platform = ?, source = ?, last_seen = ? WHERE id = ?", (platform, source, _now(), device_id))


def set_device_as_seed(conn, site_id: int, device_id: int) -> None:
    """Flag a device as the CDP discovery seed for its site.

    Clears any existing seed flag for the site first, so exactly one
    device per site is ever marked as seed. Also mirrors the hostname
    into sites.seed_device for convenience/readability.
    """
    conn.execute("UPDATE devices SET is_seed = 0 WHERE site_id = ?", (site_id,))
    conn.execute("UPDATE devices SET is_seed = 1 WHERE id = ? AND site_id = ?", (device_id, site_id))
    hostname_row = conn.execute("SELECT hostname FROM devices WHERE id = ?", (device_id,)).fetchone()
    if hostname_row:
        conn.execute("UPDATE sites SET seed_device = ? WHERE id = ?", (hostname_row["hostname"], site_id))

    hostname = hostname_row["hostname"] if hostname_row else f"device {device_id}"
    site_row = conn.execute("SELECT site_octet FROM sites WHERE id = ?", (site_id,)).fetchone()
    site_label = site_row["site_octet"] if site_row else f"site {site_id}"
    log_activity("set_cdp_seed", f"{hostname} set as CDP seed for site {site_label}", conn=conn)


def get_seed_device_for_site(conn, site_id: int):
    """Return the device row flagged as seed for a site, or None if unset."""
    return conn.execute(
        "SELECT * FROM devices WHERE site_id = ? AND is_seed = 1", (site_id,)
    ).fetchone()


def set_device_as_arp_seed(conn, site_id: int, device_id: int) -> None:
    """Flag a device as the ARP-collection seed for its site - only
    needed when it should differ from the regular CDP seed (e.g. the
    CDP seed isn't a router, or doesn't hold the ARP table you want).
    Most sites will never call this; get_arp_seed_device_for_site()
    falls back to the CDP seed automatically when no override is set.

    Clears any existing ARP-seed flag for the site first, so at most
    one device per site is ever marked. Also mirrors the hostname into
    sites.arp_seed_device for convenience/readability.
    """
    conn.execute("UPDATE devices SET is_arp_seed = 0 WHERE site_id = ?", (site_id,))
    conn.execute("UPDATE devices SET is_arp_seed = 1 WHERE id = ? AND site_id = ?", (device_id, site_id))
    hostname_row = conn.execute("SELECT hostname FROM devices WHERE id = ?", (device_id,)).fetchone()
    if hostname_row:
        conn.execute("UPDATE sites SET arp_seed_device = ? WHERE id = ?", (hostname_row["hostname"], site_id))

    hostname = hostname_row["hostname"] if hostname_row else f"device {device_id}"
    site_row = conn.execute("SELECT site_octet FROM sites WHERE id = ?", (site_id,)).fetchone()
    site_label = site_row["site_octet"] if site_row else f"site {site_id}"
    log_activity("set_arp_seed", f"{hostname} set as ARP seed for site {site_label}", conn=conn)


def clear_device_as_arp_seed(conn, site_id: int) -> None:
    """Remove any ARP-seed override for a site, reverting it to using
    the regular CDP seed by default.
    """
    conn.execute("UPDATE devices SET is_arp_seed = 0 WHERE site_id = ?", (site_id,))
    conn.execute("UPDATE sites SET arp_seed_device = NULL WHERE id = ?", (site_id,))

    site_row = conn.execute("SELECT site_octet FROM sites WHERE id = ?", (site_id,)).fetchone()
    site_label = site_row["site_octet"] if site_row else f"site {site_id}"
    log_activity("clear_arp_seed", f"ARP seed override cleared for site {site_label}", conn=conn)


def get_arp_seed_device_for_site(conn, site_id: int):
    """Return the device to poll for ARP data at a site: the explicit
    ARP-seed override if one is set, otherwise the regular CDP seed.
    Returns None only if neither is set.
    """
    override = conn.execute(
        "SELECT * FROM devices WHERE site_id = ? AND is_arp_seed = 1", (site_id,)
    ).fetchone()
    if override is not None:
        return override
    return get_seed_device_for_site(conn, site_id)


def set_device_arp_override_ip(conn, device_id: int, ip: str) -> None:
    """Set (or clear, if ip is None/empty) a device's arp_override_ip -
    an address tried first during ARP collection specifically, before
    falling through to the device's normal ranked IPs. For the shared-
    VIP case (e.g. an HSRP/VRRP pair): set this identically on BOTH
    real devices in the pair, not just whichever is currently flagged
    is_arp_seed - that way, if the seed flag ever moves to the other
    device (a refresh, a manual switch), the override is already in
    place and nothing has to be reconfigured.
    """
    conn.execute("UPDATE devices SET arp_override_ip = ? WHERE id = ?", (ip or None, device_id))

    hostname_row = conn.execute("SELECT hostname FROM devices WHERE id = ?", (device_id,)).fetchone()
    hostname = hostname_row["hostname"] if hostname_row else f"device {device_id}"
    detail = f"ARP override IP for {hostname} set to {ip}" if ip else f"ARP override IP cleared for {hostname}"
    log_activity("set_arp_override_ip", detail, conn=conn)


def set_device_marked_stale(conn, device_id: int, stale: bool) -> None:
    """Manually flag (or clear) a device as stale - independent of, and
    complementary to, the automatic age-based staleness check (a
    device's last_seen vs. its site's last_cdp_discovery - see
    dashboard_generate.py's _is_stale()). This exists for the gap the
    automatic rule can't cover on its own: a device can only look
    "missing from a later scan" once a later scan actually exists, so
    on a young deployment (or a site simply not rescanned in a while)
    the timer has nothing to compare against yet, even if you already
    know from direct knowledge that something is gone.

    Links get no flag of their own at all - a link's manual staleness
    is always derived (computed fresh, not stored) from whether either
    of its two endpoint devices is marked, so there's nothing to keep
    in sync if a device later gets un-marked.
    """
    conn.execute("UPDATE devices SET marked_stale_at = ? WHERE id = ?", (_now() if stale else None, device_id))

    hostname_row = conn.execute("SELECT hostname FROM devices WHERE id = ?", (device_id,)).fetchone()
    hostname = hostname_row["hostname"] if hostname_row else f"device {device_id}"
    detail = f"{hostname} manually marked stale" if stale else f"{hostname} stale flag cleared"
    log_activity("set_marked_stale", detail, conn=conn)


def auto_seed_singleton_sites(conn) -> list:
    """Flag the seed device for any site that has exactly one device and
    doesn't already have a seed set. No guessing involved - there's only
    one candidate, so this is safe to run automatically (unlike sites
    with multiple devices, where seed choice stays a manual call).

    Returns a list of (site_row, device_row) for everything it flagged,
    so callers can report what happened.
    """
    flagged = []
    for site in get_all_sites(conn):
        if get_seed_device_for_site(conn, site["id"]) is not None:
            continue  # already has a seed
        devices = get_devices_for_site(conn, site["id"])
        if len(devices) == 1:
            set_device_as_seed(conn, site["id"], devices[0]["id"])
            flagged.append((site, devices[0]))
    return flagged


# ---------------------------------------------------------------------
# Device IPs (ranked history - see module docstring for why this exists)
# ---------------------------------------------------------------------

# Higher wins. Sources not listed here (shouldn't normally happen) rank
# below everything, rather than erroring.
_SOURCE_RANK = {"manual": 3, "inventory_sync": 2, "cdp": 1}


def _source_rank(source: str) -> int:
    return _SOURCE_RANK.get(source, 0)


def _get_site_reference_ip(conn, site_id: int):
    """A real, representative IP for a site, used only to compare
    first-two-octet similarity when ranking a device's candidate IPs -
    NOT a site identifier itself (site_octet only stores the second
    octet, never the first, so it can't be used for this directly).
    Prefers the site's seed device's IP; falls back to any device's
    mgmt_ip at the site. Returns None if the site has no IPs at all yet.
    """
    seed = get_seed_device_for_site(conn, site_id)
    if seed and seed["mgmt_ip"]:
        return seed["mgmt_ip"]
    row = conn.execute(
        "SELECT mgmt_ip FROM devices WHERE site_id = ? AND mgmt_ip IS NOT NULL LIMIT 1",
        (site_id,),
    ).fetchone()
    return row["mgmt_ip"] if row else None


def get_best_ip_for_device(conn, device_id: int, reference_ip: str = None) -> str:
    """Return the best candidate IP for a device from its device_ips
    history: highest source rank first, then (as a tie-breaker) whether
    the IP's first two octets match reference_ip's. Returns None if the
    device has no recorded IPs at all.
    """
    rows = conn.execute(
        "SELECT ip, source FROM device_ips WHERE device_id = ?", (device_id,)
    ).fetchall()
    if not rows:
        return None

    ref_prefix = None
    if reference_ip:
        parts = reference_ip.split(".")
        if len(parts) >= 2:
            ref_prefix = f"{parts[0]}.{parts[1]}."

    def sort_key(row):
        octet_match = 1 if (ref_prefix and row["ip"].startswith(ref_prefix)) else 0
        return (_source_rank(row["source"]), octet_match)

    ranked = sorted(rows, key=sort_key, reverse=True)
    return ranked[0]["ip"]


def get_ips_for_device(conn, device_id: int):
    """All recorded IPs for a device, highest-ranked first (same
    ordering logic as get_best_ip_for_device, without collapsing to
    just one result) - useful for a connection step that wants to try
    every known IP in order rather than just the top pick.
    """
    reference_ip = None
    device = conn.execute("SELECT site_id FROM devices WHERE id = ?", (device_id,)).fetchone()
    if device:
        reference_ip = _get_site_reference_ip(conn, device["site_id"])

    rows = conn.execute(
        "SELECT ip, source FROM device_ips WHERE device_id = ?", (device_id,)
    ).fetchall()

    ref_prefix = None
    if reference_ip:
        parts = reference_ip.split(".")
        if len(parts) >= 2:
            ref_prefix = f"{parts[0]}.{parts[1]}."

    def sort_key(row):
        octet_match = 1 if (ref_prefix and row["ip"].startswith(ref_prefix)) else 0
        return (_source_rank(row["source"]), octet_match)

    return [row["ip"] for row in sorted(rows, key=sort_key, reverse=True)]


def _refresh_cached_mgmt_ip(conn, device_id: int) -> None:
    """Recompute devices.mgmt_ip from device_ips and write the cache.
    Internal - called automatically by record_device_ip(); nothing else
    should need to call this directly.
    """
    device = conn.execute("SELECT site_id FROM devices WHERE id = ?", (device_id,)).fetchone()
    if not device:
        return
    reference_ip = _get_site_reference_ip(conn, device["site_id"])
    best = get_best_ip_for_device(conn, device_id, reference_ip=reference_ip)
    if best:
        conn.execute("UPDATE devices SET mgmt_ip = ? WHERE id = ?", (best, device_id))


def record_device_ip(conn, device_id: int, ip: str, source: str, raw_source_data: str = None) -> None:
    """Record an observed IP for a device. Never destructively
    overwrites - if this exact IP is already known for this device,
    its source is only upgraded (never downgraded) to whatever ranks
    higher, and last_seen always refreshes either way. Automatically
    recomputes and updates the device's cached mgmt_ip afterward.

    This is the ONLY function that should feed device_ips - nothing
    else should INSERT/UPDATE that table directly.
    """
    if not ip:
        return

    existing = conn.execute(
        "SELECT source FROM device_ips WHERE device_id = ? AND ip = ?", (device_id, ip)
    ).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO device_ips (device_id, ip, source, last_seen, raw_source_data) VALUES (?, ?, ?, ?, ?)",
            (device_id, ip, source, _now(), raw_source_data),
        )
    elif _source_rank(source) >= _source_rank(existing["source"]):
        conn.execute(
            "UPDATE device_ips SET source = ?, last_seen = ?, raw_source_data = ? WHERE device_id = ? AND ip = ?",
            (source, _now(), raw_source_data, device_id, ip),
        )
    else:
        # A lower-priority source re-reported an IP we already trust
        # more from elsewhere - keep the existing source tag, but it
        # was still just seen again, so refresh last_seen.
        conn.execute(
            "UPDATE device_ips SET last_seen = ? WHERE device_id = ? AND ip = ?",
            (_now(), device_id, ip),
        )

    _refresh_cached_mgmt_ip(conn, device_id)


# ---------------------------------------------------------------------
# Links (CDP adjacencies)
# ---------------------------------------------------------------------

def upsert_link(
    conn,
    site_id: int,
    device_a_id: int,
    device_b_id: int,
    local_intf: str = None,
    remote_intf: str = None,
    source: str = "cdp",
    raw_source_data: str = None,
) -> int:
    """Insert or refresh a CDP link between two devices. Returns link_id.

    Same-site CDP walks discover most links from BOTH ends (device A
    reports seeing B; device B separately reports seeing A) - which
    would otherwise create two rows for what's really one physical
    cable: (A, B, local=X, remote=Y) and (B, A, local=Y, remote=X).
    Before inserting, check whether the MIRROR of this link (device_a/
    device_b swapped, AND local_intf/remote_intf swapped to match)
    already exists - if so, this is the same cable seen from the other
    end, so just refresh that existing row instead of creating a
    second one. A genuine second physical cable between the same two
    devices always uses a different port on at least one end, so it's
    never mistaken for this case.
    """
    mirror = conn.execute(
        """
        SELECT id FROM links
        WHERE site_id = ? AND device_a_id = ? AND device_b_id = ?
          AND IFNULL(local_intf, '') = IFNULL(?, '')
          AND IFNULL(remote_intf, '') = IFNULL(?, '')
        """,
        (site_id, device_b_id, device_a_id, remote_intf, local_intf),
    ).fetchone()
    if mirror is not None:
        conn.execute(
            "UPDATE links SET source = ?, last_seen = ?, raw_source_data = ? WHERE id = ?",
            (source, _now(), raw_source_data, mirror["id"]),
        )
        return mirror["id"]

    conn.execute(
        """
        INSERT INTO links (site_id, device_a_id, device_b_id, local_intf, remote_intf, source, last_seen, raw_source_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(site_id, device_a_id, device_b_id, local_intf, remote_intf) DO UPDATE SET
            source          = excluded.source,
            last_seen       = excluded.last_seen,
            raw_source_data = excluded.raw_source_data
        """,
        (site_id, device_a_id, device_b_id, local_intf, remote_intf, source, _now(), raw_source_data),
    )
    row = conn.execute(
        """
        SELECT id FROM links
        WHERE site_id = ? AND device_a_id = ? AND device_b_id = ?
          AND IFNULL(local_intf, '') = IFNULL(?, '')
          AND IFNULL(remote_intf, '') = IFNULL(?, '')
        """,
        (site_id, device_a_id, device_b_id, local_intf, remote_intf),
    ).fetchone()
    return row["id"]


def get_links_for_site(conn, site_id: int):
    return conn.execute(
        """
        SELECT l.*, da.hostname AS device_a_hostname, db.hostname AS device_b_hostname
        FROM links l
        JOIN devices da ON l.device_a_id = da.id
        JOIN devices db ON l.device_b_id = db.id
        WHERE l.site_id = ? AND da.deleted_at IS NULL AND db.deleted_at IS NULL
        """,
        (site_id,),
    ).fetchall()


# ---------------------------------------------------------------------
# ARP entries
# ---------------------------------------------------------------------

def upsert_arp_entry(
    conn,
    site_id: int,
    ip: str,
    mac: str,
    vrf: str = "",
    device_id: int = None,
    source: str = "cdp",
    raw_source_data: str = None,
) -> int:
    """Insert or refresh an ARP entry for a site. Returns arp entry id.

    vrf defaults to '' (empty string), meaning the global/default
    routing table - deliberately NOT NULL, since SQL treats every NULL
    as distinct from every other NULL for uniqueness purposes, which
    would silently break deduplication for every global-table entry.
    Pass the real VRF name for anything pulled from a specific VRF's
    ARP table.
    """
    conn.execute(
        """
        INSERT INTO arp_entries (site_id, device_id, vrf, ip, mac, source, last_seen, raw_source_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(site_id, vrf, ip, mac) DO UPDATE SET
            device_id       = excluded.device_id,
            source          = excluded.source,
            last_seen       = excluded.last_seen,
            raw_source_data = excluded.raw_source_data
        """,
        (site_id, device_id, vrf, ip, mac, source, _now(), raw_source_data),
    )
    row = conn.execute(
        "SELECT id FROM arp_entries WHERE site_id = ? AND vrf = ? AND ip = ? AND mac = ?",
        (site_id, vrf, ip, mac),
    ).fetchone()
    return row["id"]


def upsert_client(
    conn,
    site_id: int,
    mac: str,
    device_id: int = None,
    interface: str = None,
    vlan: str = None,
    device_type: str = None,
    source: str = "mac-table",
    raw_source_data: str = None,
) -> int:
    """Insert or refresh a client (a MAC seen as a genuine edge/end-
    device port, not an inter-switch link - that decision is made by
    the correlation pass that calls this, not here). One row per MAC
    per site; a MAC already known at this site has its location
    REFRESHED (device/interface/vlan/last_seen/mac's own stored
    formatting all overwritten with the latest values) rather than
    creating a second row - "last processed wins" is the deliberate,
    accepted behavior here, matching how a real L2 network's own MAC
    tables self-correct as old entries age out, rather than something
    this needs to resolve itself.

    Compares MACs via mac_parser.normalize_mac() rather than a plain
    string match, since the same physical MAC can be reported in
    different separator styles/case across platforms and even
    different commands on the same platform - a raw string comparison
    would otherwise silently treat one real device as two separate
    client rows. This is why the actual matching happens here in
    Python rather than via the schema's UNIQUE(site_id, mac) alone,
    which only catches an exact raw-string repeat.

    device_type (e.g. "cisco_phone", set via a separate CDP-based
    correlation step, not from the MAC table itself) is preserved
    across a later refresh that doesn't provide one - a plain MAC-
    table sighting with no type info shouldn't erase a type already
    learned from CDP.
    """
    normalized_new = mac_parser.normalize_mac(mac)

    existing = conn.execute(
        "SELECT id, mac FROM clients WHERE site_id = ?", (site_id,)
    ).fetchall()
    match_id = None
    for row in existing:
        if mac_parser.normalize_mac(row["mac"]) == normalized_new:
            match_id = row["id"]
            break

    if match_id is not None:
        conn.execute(
            """
            UPDATE clients
            SET mac = ?, device_id = ?, interface = ?, vlan = ?,
                device_type = COALESCE(?, device_type),
                source = ?, last_seen = ?, raw_source_data = ?
            WHERE id = ?
            """,
            (mac, device_id, interface, vlan, device_type, source, _now(), raw_source_data, match_id),
        )
        return match_id

    cur = conn.execute(
        """
        INSERT INTO clients (site_id, mac, device_id, interface, vlan, device_type, source, last_seen, raw_source_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (site_id, mac, device_id, interface, vlan, device_type, source, _now(), raw_source_data),
    )
    return cur.lastrowid


def get_clients_for_site(conn, site_id: int):
    return conn.execute(
        """
        SELECT clients.* FROM clients
        LEFT JOIN devices ON clients.device_id = devices.id
        WHERE clients.site_id = ? AND (devices.deleted_at IS NULL OR clients.device_id IS NULL)
        ORDER BY clients.mac
        """,
        (site_id,),
    ).fetchall()


def get_all_clients(conn):
    """Every client across every site, unfiltered beyond hiding
    anything attached to a soft-deleted device - callers filter by
    device_type or anything else as needed (e.g. the CUCM/VTC
    enrichment script only cares about 'cisco_phone'/'vtc' rows, but
    this stays a plain, general-purpose accessor rather than baking
    that specific filter in here).
    """
    return conn.execute(
        """
        SELECT clients.* FROM clients
        LEFT JOIN devices ON clients.device_id = devices.id
        WHERE devices.deleted_at IS NULL OR clients.device_id IS NULL
        ORDER BY clients.mac
        """
    ).fetchall()


def stage_mac_table_raw(conn, site_id: int, device_id: int, raw_output: str) -> None:
    """Stages a device's raw 'show mac address-table' output for a
    later correlation pass (see mac_table_raw's own schema comment for
    why this can't be decided mid-walk). One row per device - a later
    collection for the same device REPLACES its previous staged
    output, not accumulated as history.
    """
    conn.execute(
        """
        INSERT INTO mac_table_raw (site_id, device_id, raw_output, collected_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            site_id      = excluded.site_id,
            raw_output   = excluded.raw_output,
            collected_at = excluded.collected_at
        """,
        (site_id, device_id, raw_output, _now()),
    )


def get_staged_mac_tables_for_site(conn, site_id: int):
    return conn.execute(
        "SELECT * FROM mac_table_raw WHERE site_id = ?", (site_id,)
    ).fetchall()


def upsert_phone_enrichment_status(conn, mac: str, device_name: str, ris_ip: str, ris_status: str,
                                    phone_number: str = None) -> None:
    """Refreshes the FAST, always-current fields from RIS's bulk query
    (IP + registration status + directory number) - meant to be called
    for every tracked phone/VTC on every enrichment script run,
    regardless of whether model/serial are already known for that MAC.
    Deliberately has no skip logic at all: ris_status can genuinely
    flip over time (a device going from Registered to Unregistered and
    back), and phone_number can too (an extension gets reassigned), so
    both need to stay current every run, unlike the slower per-device
    pull below. Creates the row if this MAC has never been enriched
    before.
    """
    mac = mac_parser.normalize_mac(mac)
    conn.execute(
        """
        INSERT INTO phone_enrichment (mac, device_name, ris_ip, ris_status, phone_number)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(mac) DO UPDATE SET
            device_name  = excluded.device_name,
            ris_ip       = excluded.ris_ip,
            ris_status   = excluded.ris_status,
            phone_number = excluded.phone_number
        """,
        (mac, device_name, ris_ip, ris_status, phone_number),
    )


def record_phone_serial_pull(conn, mac: str, model: str = None, serial_number: str = None) -> None:
    """Records the outcome of a per-device model/serial pull attempt
    against a phone_enrichment row that already exists (created first
    by upsert_phone_enrichment_status(), which always runs before this
    for every tracked device). Always stamps last_attempted, whether
    or not the pull actually returned anything - that's what lets a
    permanently-unreachable legacy device get tried exactly once
    rather than retried on every future run. last_enriched is only
    stamped when the pull genuinely returned a model and/or serial,
    distinguishing "we tried and got nothing" from "we tried and got
    something real." A None model/serial here leaves any existing
    value in place (COALESCE) rather than blanking out a previously-
    known value with nothing.
    """
    mac = mac_parser.normalize_mac(mac)
    now = _now()
    got_something = bool(model or serial_number)
    conn.execute(
        """
        UPDATE phone_enrichment
        SET model          = COALESCE(?, model),
            serial_number  = COALESCE(?, serial_number),
            last_attempted = ?,
            last_enriched  = CASE WHEN ? THEN ? ELSE last_enriched END
        WHERE mac = ?
        """,
        (model, serial_number, now, got_something, now, mac),
    )


def get_phone_enrichment(conn, mac: str):
    mac = mac_parser.normalize_mac(mac)
    return conn.execute("SELECT * FROM phone_enrichment WHERE mac = ?", (mac,)).fetchone()


def get_all_phone_enrichment(conn):
    return conn.execute("SELECT * FROM phone_enrichment").fetchall()


def get_arp_for_site(conn, site_id: int):
    return conn.execute(
        """
        SELECT arp_entries.* FROM arp_entries
        LEFT JOIN devices ON arp_entries.device_id = devices.id
        WHERE arp_entries.site_id = ? AND (devices.deleted_at IS NULL OR arp_entries.device_id IS NULL)
        ORDER BY arp_entries.vrf, arp_entries.ip
        """,
        (site_id,),
    ).fetchall()



# ---------------------------------------------------------------------
# Device merge support (used by merge_duplicate_devices.py)
# ---------------------------------------------------------------------
# These exist for the one specific case where two device rows turn out
# to be the same physical device (e.g. a real inventory-known device
# and a CDP-discovered duplicate that got filed under the wrong site).
# Merging is: migrate history, repoint references,
# then delete the now-childless duplicate row. Foreign keys are
# enforced, so a duplicate can't be deleted while device_ips/links/
# arp_entries still reference it - these functions exist to clear
# those references first.

def get_device_ip_rows(conn, device_id: int):
    """Raw device_ips rows (ip, source, raw_source_data) for a device -
    used to replay a duplicate's IP history into the real device via
    record_device_ip(), so source-priority ranking is respected rather
    than just copied blindly.
    """
    return conn.execute(
        "SELECT ip, source, raw_source_data FROM device_ips WHERE device_id = ?", (device_id,)
    ).fetchall()


def delete_device_ip_rows(conn, device_id: int) -> None:
    conn.execute("DELETE FROM device_ips WHERE device_id = ?", (device_id,))


def get_links_referencing_device(conn, device_id: int):
    """Every link row where this device appears as either side."""
    return conn.execute(
        "SELECT * FROM links WHERE device_a_id = ? OR device_b_id = ?", (device_id, device_id)
    ).fetchall()


def delete_link(conn, link_id: int) -> None:
    conn.execute("DELETE FROM links WHERE id = ?", (link_id,))


def reassign_arp_entries_device(conn, old_device_id: int, new_device_id: int) -> None:
    """Repoint arp_entries.device_id from old to new. No conflict risk -
    device_id isn't part of arp_entries' uniqueness key (site_id, ip,
    mac), so this is a plain reassignment, not an upsert.
    """
    conn.execute("UPDATE arp_entries SET device_id = ? WHERE device_id = ?", (new_device_id, old_device_id))


def delete_device(conn, device_id: int) -> None:
    """Delete a device row outright. Caller must have already migrated
    or removed any device_ips/links/arp_entries referencing it first -
    foreign keys are enforced, so this raises if child rows remain.
    """
    conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))


if __name__ == "__main__":
    # Running this file directly just (re)creates an empty sad.db in the
    # current directory - useful as a sanity check that the schema is valid.
    init_db()
    print(f"Initialized {DB_PATH}")
