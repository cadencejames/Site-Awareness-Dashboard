"""stale_check.py - read-only. Prints every device the dashboard's rule
calls stale, per site, with the reason, so it can be compared against
the dashboard page and the Site Manager list. Usage:
    python stale_check.py            (all sites)
    python stale_check.py 10.1.1     (one site octet)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utilities"))
import db
import dashboard_generate as dg

only = sys.argv[1] if len(sys.argv) > 1 else None
total = 0
print("Database:", os.path.abspath(db.DB_PATH))
with db.get_conn() as conn:
    # Same arithmetic as the index tile (no deleted_at filter, no
    # Unassigned), so any difference from the total below is visible.
    tile = 0
    tile_rows = []
    tile_deleted = 0
    for s in conn.execute("SELECT * FROM sites WHERE site_octet != 'unassigned'").fetchall():
        if not s["last_cdp_discovery"]:
            continue
        for d in conn.execute("SELECT id, hostname, last_seen, marked_stale_at, deleted_at FROM devices WHERE site_id = ?", (s["id"],)):
            if dg._device_is_stale(d, s["last_cdp_discovery"]):
                tile += 1
                tile_deleted += 1 if d["deleted_at"] else 0
                tile_rows.append((s["site_octet"], d["id"], d["hostname"], d["last_seen"], d["marked_stale_at"], d["deleted_at"]))
    print(f"Index tile formula: {tile} stale ({tile_deleted} of them soft-deleted)")
    print("Every device the tile counts (octet | id | hostname | last_seen | marked_stale_at | deleted_at):")
    site_by_octet = {r["site_octet"]: r for r in conn.execute("SELECT * FROM sites")}
    visible = {}
    for r in tile_rows:
        srow = site_by_octet[r[0]]
        if srow["id"] not in visible:
            visible[srow["id"]] = {d["id"]: d for d in db.get_devices_for_site(conn, srow["id"])}
        d = visible[srow["id"]].get(r[1])
        if d is None:
            verdict = "NOT RETURNED by db.get_devices_for_site"
        else:
            verdict = "reason=" + str(dg.device_stale_reason(d, srow["last_cdp_discovery"]))
        print("   " + " | ".join(repr(x) if x in ("", None) else str(x) for x in r) + "  -> " + verdict)
    print()
    for s in conn.execute("SELECT * FROM sites WHERE deleted_at IS NULL ORDER BY site_octet").fetchall():
        if only and s["site_octet"] != only:
            continue
        rows = []
        for d in db.get_devices_for_site(conn, s["id"]):
            why = dg.device_stale_reason(d, s["last_cdp_discovery"])
            if why:
                rows.append((d["hostname"], d["last_seen"], why))
        if not rows:
            continue
        print(f"\n{s['site_octet']}  last CDP scan: {s['last_cdp_discovery']}  ({len(rows)} stale)")
        for h, ls, why in rows:
            print(f"   {h:<40} last_seen={ls}  {why}")
        total += len(rows)
print(f"\nTotal stale devices: {total}")