#!/usr/bin/env python3
"""
pull_orion.py - pulls node Down/Up events and the node list from SolarWinds
Orion and stores them in sad.db, so SAD keeps the history after Orion deletes
events older than its retention (30 days on this server).

Run from the project root, like the other SAD scripts:

    python pull_orion.py                 fetch from Orion and show what a pull would store (changes nothing)
    python pull_orion.py --apply         store it
    python pull_orion.py --status        show what is already stored (does not contact Orion)
    python pull_orion.py --days 14       look back 14 days instead of 30 (the most Orion keeps is its retention)
    python pull_orion.py --force --apply store even though a safety check failed (see below)
    python pull_orion.py --prompt        type the username and password instead of using the stored 'tacacs' login

Pulling twice is harmless: each event has Orion's own EventID, so one already
stored is skipped. Pull at least once a month; events Orion has already deleted
cannot be recovered, and a longer gap shows up under --status.

Safety checks before anything is stored (--force skips them):
  - Orion's own count of Down and Up events must match what was fetched
  - at least one Orion node must have been read

Settings (optional) in settings.json:
    "orion": {"host": "orion.example.local", "port": 17774,
              "verify_tls": false, "timeout_seconds": 120}
Command-line options override them.
"""
import argparse
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "utilities"))

import db  # noqa: E402
import orion_client as oc  # noqa: E402
import orion_match as om  # noqa: E402
import orion_parser as op  # noqa: E402
import outage_db  # noqa: E402
import write_queue  # noqa: E402

STAMP = "%Y-%m-%d %H:%M"
STALE_AFTER_DAYS = 20      # --status warns when the last pull is older than this


def _settings():
    try:
        import settings_loader
        return settings_loader.get_section("orion")
    except Exception:
        return {}


def _configured_host():
    return str(_settings().get("host") or "").strip()


def get_credentials(force_prompt=False):
    """(username, password) from the stored 'tacacs' login, or typed in."""
    if not force_prompt:
        try:
            import credential_loader
            creds = credential_loader.prompt_and_load()
            tacacs = creds.get("tacacs") or {}
            user = (tacacs.get("username") or {}).get("value")
            pw = (tacacs.get("password") or {}).get("value")
            if user and pw:
                print("Using the stored 'tacacs' login.")
                return user, pw
            print("The credential store has no complete 'tacacs' login; asking instead.", file=sys.stderr)
        except ImportError:
            print("credential_loader not found; asking instead.", file=sys.stderr)
        except SystemExit:
            print("Could not unlock the credential store; asking instead.", file=sys.stderr)
        except Exception as exc:
            print(f"Could not read the credential store ({exc.__class__.__name__}); asking instead.", file=sys.stderr)
    user = input("Orion username: ").strip()
    return user, getpass.getpass("Orion password: ")


def _best_effort(fn, default):
    try:
        return fn()
    except oc.OrionError:
        return default


def fetch_pull(client, conn, days):
    """Read everything a pull needs from Orion and SAD, without writing anything.

    Returns {"payload": {"nodes", "events", "pull"} (ready for outage_db.store_orion_pull),
             "problems": [...] (reasons not to store it), "warnings": [...], "info": {...}}."""
    now = client.server_now()
    nodes, node_errors = client.fetch_nodes()
    events, event_errors = client.fetch_events(days)
    type_counts = _best_effort(lambda: client.event_type_counts(days), {})
    span = _best_effort(client.event_span, {}) or {}
    devices = om.load_sad_devices(conn)
    result = om.match_nodes(nodes, devices)

    problems, warnings = [], []
    if not nodes:
        problems.append("Orion returned no readable nodes.")
    fetched = {op.EVENT_DOWN: 0, op.EVENT_UP: 0}
    for e in events:
        fetched[e["type"]] = fetched.get(e["type"], 0) + 1
    for etype, label in ((op.EVENT_DOWN, "Node Down"), (op.EVENT_UP, "Node Up")):
        reported, got = type_counts.get(etype, 0), fetched.get(etype, 0)
        if abs(reported - got) > max(2, reported // 100):
            problems.append(f"{label} ({etype}): Orion reports {reported} in the last {days} days but {got} were fetched.")
    if client.last_event_duplicates:
        warnings.append(f"{client.last_event_duplicates} event(s) came back more than once and were dropped.")
    if node_errors:
        warnings.append(f"{len(node_errors)} Orion node row(s) could not be read. First: {node_errors[0]}")
    if event_errors:
        warnings.append(f"{len(event_errors)} Orion event row(s) could not be read. First: {event_errors[0]}")
    no_id = [e for e in events if e["event_id"] is None]
    if no_id:
        warnings.append(f"{len(no_id)} event(s) had no EventID and were not stored.")
    lacking = om.sites_lacking_seed_node(devices, result)
    if lacking:
        warnings.append(f"{len(lacking)} SAD site(s) have no deciding node in Orion, so their outages cannot be tracked.")
    if result["ambiguous"]:
        warnings.append(f"{len(result['ambiguous'])} Orion node(s) fit several SAD devices and were left unmatched.")

    lookback = now - timedelta(days=days)
    earliest = span.get("earliest")
    covered_from = max(lookback, earliest) if earliest else lookback
    node_rows = []
    for n in nodes:
        match = result["matches"].get(n["node_id"])
        node_rows.append({"node_id": n["node_id"], "caption": n["caption"], "ip": n["ip"], "status": n["status"],
                          "device_id": match["device_id"] if match else None,
                          "match_method": match["method"] if match else None})
    event_rows = [{"event_id": e["event_id"], "node_id": e["node_id"], "time": outage_db.fmt_time(e["time"]), "type": e["type"]}
                  for e in events if e["event_id"] is not None]
    pull = {"server_now": outage_db.fmt_time(now), "days": days, "covered_from": outage_db.fmt_time(covered_from),
            "covered_to": outage_db.fmt_time(now), "earliest_event": outage_db.fmt_time(earliest) if earliest else None,
            "events_fetched": len(event_rows), "nodes_fetched": len(node_rows), "nodes_matched": len(result["matches"]),
            "note": None}
    outage_db.validate_pull(node_rows, event_rows, pull)
    info = {"now": now, "nodes": len(nodes), "matched": len(result["matches"]), "ignored": len(result["unmatched"]),
            "down": fetched[op.EVENT_DOWN], "up": fetched[op.EVENT_UP], "reported": type_counts,
            "covered_from": covered_from, "earliest": earliest}
    return {"payload": {"nodes": node_rows, "events": event_rows, "pull": pull},
            "problems": problems, "warnings": warnings, "info": info}


def show_status(conn):
    counts = outage_db.event_counts(conn)
    print(f"Stored: {counts['events']} events ({counts['down']} Down, {counts['up']} Up), "
          f"{counts['nodes']} Orion nodes ({counts['nodes_matched']} matched to SAD devices).")
    last = outage_db.last_pull(conn)
    if last is None:
        print("No pull has been stored yet. Run: python pull_orion.py --apply")
        return
    pulled = datetime.fromisoformat(last["pulled_at"])
    age = datetime.now(timezone.utc) - pulled
    by = f" by {last['pulled_by']}" if last["pulled_by"] else ""
    print(f"Last pull: {last['pulled_at']} UTC{by}, {age.days} day(s) ago; Orion's clock then read {last['server_now']}; "
          f"{last['events_fetched']} events fetched, {last['events_new']} new.")
    if age.days >= STALE_AFTER_DAYS:
        print(f"  WARNING: the last pull is {age.days} days old. Orion keeps 30 days, so pull again soon.")
    spans = outage_db.coverage(conn)
    print("Events cover (Orion server time):")
    for start, end in spans:
        print(f"  {start:{STAMP}} -> {end:{STAMP}}")
    if len(spans) > 1:
        print(f"  WARNING: {len(spans) - 1} gap(s) in coverage. Outages in those periods cannot be known.")
    pulls = outage_db.list_pulls(conn, 5)
    print("Recent pulls:")
    for p in pulls:
        print(f"  #{p['id']:<4} {p['pulled_at']} UTC  fetched {p['events_fetched']:5d}  new {p['events_new']:5d}  covers {p['covered_from'][:16]} -> {p['covered_to'][:16]}")


def run(args):
    db.init_db()
    if args.status:
        with db.get_conn() as conn:
            show_status(conn)
        return 0
    host = args.host or _configured_host()
    if not host:
        print("ERROR: no Orion host. Pass --host NAME, or add \"orion\": {\"host\": \"NAME\"} to settings.json.", file=sys.stderr)
        return 1
    if args.days < 1:
        print("ERROR: --days must be at least 1.", file=sys.stderr)
        return 1
    user, password = get_credentials(args.prompt)
    client = oc.OrionClient(host, user, password, port=args.port, verify_tls=args.verify_tls, timeout=args.timeout,
                            scheme="http" if args.http else "https", page_size=args.page_size)
    try:
        with db.get_conn() as conn:
            fetched = fetch_pull(client, conn, args.days)
            info, payload = fetched["info"], fetched["payload"]
            print(f"Connected to {host}. Orion server time: {info['now']:%Y-%m-%d %H:%M:%S}")
            print(f"Orion nodes: {info['nodes']}  (SAD devices: {info['matched']}, ignored: {info['ignored']})")
            print(f"Events fetched, last {args.days} days: {info['down']} Down, {info['up']} Up   "
                  f"(Orion reports {info['reported'].get(op.EVENT_DOWN, 0)} and {info['reported'].get(op.EVENT_UP, 0)})")
            print(f"This pull covers {info['covered_from']:{STAMP}} -> {info['now']:{STAMP}}")
            for line in fetched["warnings"]:
                print(f"  note: {line}")
            if fetched["problems"]:
                print("Safety check failed:")
                for line in fetched["problems"]:
                    print(f"  - {line}")
                if not args.force:
                    print("Nothing was stored. Fix the cause, or use --force if you are sure.")
                    return 1
                print("  --force given: continuing anyway.")
            summary = None
            if not args.apply:
                summary = outage_db.store_orion_pull(conn, **payload)
                conn.rollback()
        if args.apply:
            summary = write_queue.queue_and_wait("store_orion_pull", timeout=120, **payload)
    except oc.OrionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: storing the pull failed: {exc}", file=sys.stderr)
        return 1
    verb = "Stored" if args.apply else "Would store"
    print(f"{verb}: {summary['events_new']} new event(s) of {summary['events_seen']} fetched "
          f"({summary['events_seen'] - summary['events_new']} already stored), {summary['nodes_new']} new node(s).")
    if not args.apply:
        print("Dry run only, nothing was changed. Add --apply to store it.")
    return 0


def parse_args(argv):
    cfg = _settings()
    p = argparse.ArgumentParser(description="Pull Orion node Down/Up events into SAD's database.")
    p.add_argument("--host", help="Orion server name or IP (or set orion.host in settings.json)")
    p.add_argument("--port", type=int, default=int(cfg.get("port") or oc.DEFAULT_PORT))
    p.add_argument("--days", type=int, default=30, help="how many days back to fetch (default 30)")
    p.add_argument("--apply", action="store_true", help="store the pull (without it, only shows what would be stored)")
    p.add_argument("--force", action="store_true", help="store even if a safety check failed")
    p.add_argument("--status", action="store_true", help="show what is stored; does not contact Orion")
    p.add_argument("--prompt", action="store_true", help="type the username and password instead of using the stored login")
    p.add_argument("--verify-tls", action="store_true", default=bool(cfg.get("verify_tls", False)),
                   help="check Orion's TLS certificate (off by default; most Orion servers are self-signed)")
    p.add_argument("--timeout", type=float, default=float(cfg.get("timeout_seconds") or 120))
    p.add_argument("--page-size", type=int, default=2000)
    p.add_argument("--http", action="store_true", help="plain HTTP instead of HTTPS (only for the offline test server)")
    return p.parse_args(argv)


def main(argv=None):
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())