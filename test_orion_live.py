#!/usr/bin/env python3
"""
test_orion_live.py - read-only probe of a live SolarWinds Orion server.

Run it from the SAD project root on the machine that can reach Orion:

    python test_orion_live.py --host orion.example.local --days 30 --scrub

It signs in with the stored 'tacacs' login (use --prompt to type a username and
password instead), asks Orion a handful of read-only questions, and prints a
report. Nothing is written to Orion or to SAD's database, and the password is
never printed or saved.

Only devices SAD knows are reported on. Each Orion node is matched to a SAD
device by IP address (every IP SAD holds for the device), then by host name;
all other nodes (servers, workstations, ...) are counted and ignored. Use
--all-nodes to switch the filter off. The database is read from sad.db in the
project root, or from --sad-db PATH.

What it reports:
  - Orion's clock, and which node properties this Orion version offers
  - how many Orion nodes match SAD devices, how, and what could not be matched
  - SAD sites with no deciding node (no seed flagged, or the seed is not in Orion)
  - how far back Orion's event history goes (retention), which event types it
    records, and whether Down/Up events pair up cleanly for SAD devices
  - with --octet N: every SAD device and Down/Up event for the site whose SAD
    site octet is N

Sharing the output: add --scrub and device names, IP addresses and site numbers
are replaced with Node-1, Site-2, ... Counts, statuses, event types and times
are kept. --dump FILE saves a small sample (one site with --octet, otherwise
the first 50 matched nodes) as JSON that can serve as realistic test data; with
--scrub it contains no names or addresses either.

Settings (optional) in settings.json:
    "orion": {"host": "orion.example.local", "port": 17774,
              "verify_tls": false, "timeout_seconds": 120}
Command-line options override them.
"""
import argparse
import getpass
import json
import os
import sys
from collections import Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "utilities"))

import orion_client as oc  # noqa: E402
import orion_match as om  # noqa: E402
import orion_parser as op  # noqa: E402

STAMP = "%Y-%m-%d %H:%M"


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


def fmt_duration(td):
    secs = max(int(td.total_seconds()), 0)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    parts = []
    if d:
        parts.append(f"{d}d")
    if d or h:
        parts.append(f"{h}h")
    parts.append(f"{rem // 60}m")
    return " ".join(parts)


def best_effort(fn, default):
    try:
        return fn()
    except oc.OrionError:
        return default


def heading(text):
    print(f"\n== {text} ==")


def simple_intervals(node_events):
    """Raw down intervals for one node, no thresholds: [(start|None, end|None)]."""
    out, start, down = [], None, False
    for e in sorted(node_events, key=op.event_sort_key):
        if e["type"] == op.EVENT_DOWN and not down:
            start, down = e["time"], True
        elif e["type"] == op.EVENT_UP:
            out.append((start, e["time"]) if down else (None, e["time"]))   # no Down seen: it was already down
            down = False
    if down:
        out.append((start, None))
    return out


def load_sad(args):
    """(devices, note). devices is None when SAD filtering is off or unavailable."""
    if args.all_nodes:
        return None, "SAD filtering is off (--all-nodes): every Orion node is included."
    if not os.path.exists(args.sad_db):
        return None, f"SAD database not found at {args.sad_db}; showing every Orion node. Use --sad-db PATH."
    import db
    with db.get_conn(args.sad_db) as conn:
        return om.load_sad_devices(conn), None


def run(client, args, scrub, host):
    # ---- 1. connection and properties ----------------------------------
    heading("Connection")
    now = client.server_now()
    shown_host = "<orion host>" if scrub.enabled else host
    print(f"Connected to {shown_host}.")
    print(f"Orion server time: {now:%Y-%m-%d %H:%M:%S}   (this machine: {datetime.now():%Y-%m-%d %H:%M:%S})")
    print("Event times below are on the Orion server's clock.")
    have, missing = client.node_fields()
    print(f"Node properties available: {', '.join(have)}")
    print(f"Not on this Orion version: {', '.join(missing) if missing else 'none'}")

    # ---- 2. nodes and the SAD match ------------------------------------
    nodes, node_errors = client.fetch_nodes()
    status_names = best_effort(client.status_names, {})
    node_by_id = {n["node_id"]: n for n in nodes}
    heading("Nodes")
    print(f"Orion nodes: {len(nodes)}")
    if node_errors:
        print(f"  {len(node_errors)} row(s) could not be understood. First few:")
        for message in node_errors[:3]:
            print(f"    {message}")
    devices, note = load_sad(args)
    if note:
        print(note)

    view = {}      # node_id -> {"node", "site", "device", "method"} for every node that counts
    if devices is None:
        for n in nodes:
            site = str(op.second_octet(n["ip"])) if n["ip"] else "?"
            view[n["node_id"]] = {"node": n, "site": site, "device": None, "method": "-"}
        dev_by_id = {}
    else:
        result = om.match_nodes(nodes, devices)
        dev_by_id = {d["device_id"]: d for d in devices}
        for nid, info in result["matches"].items():
            device = dev_by_id[info["device_id"]]
            view[nid] = {"node": node_by_id[nid], "site": device["site_octet"], "device": device, "method": info["method"]}
        methods = Counter(info["method"] for info in result["matches"].values())
        sites_in_sad = len({d["site_octet"] for d in devices})
        print(f"SAD devices loaded: {len(devices)} at {sites_in_sad} sites")
        print(f"Orion nodes that are SAD devices: {len(view)}  "
              f"(by IP {methods['ip']}, by IP and name {methods['ip+name']}, by host name {methods['hostname']})")
        print(f"Orion nodes ignored (not in SAD): {len(result['unmatched'])}")
        if result["ambiguous"]:
            print(f"Orion nodes that could not be decided (several SAD devices fit): {len(result['ambiguous'])}")
        if result["multi_node_devices"]:
            print(f"SAD devices matched by more than one Orion node: {len(result['multi_node_devices'])}")
        if result["octet_mismatch"]:
            print(f"Matched nodes whose IP second octet differs from the SAD site: {len(result['octet_mismatch'])}")
        print(f"SAD devices with no Orion node: {len(result['devices_without_node'])}")
        lacking = om.sites_lacking_seed_node(devices, result)
        print(f"SAD sites with no deciding node for outage tracking: {len(lacking)}")
        for problem in lacking[:10]:
            site_text = scrub.label("Site", problem["site_octet"]) if scrub.enabled else f"site {problem['site_octet']} {problem['site_name']}".rstrip()
            print(f"    {site_text}: {problem['reason']}")
        if len(lacking) > 10:
            print(f"    ... {len(lacking) - 10} more")

    kept = [v["node"] for v in view.values()]
    counts = Counter(n["status"] for n in kept)
    print("Status of the nodes that count:")
    for status, count in sorted(counts.items(), key=lambda kv: (kv[0] is None, kv[0])):
        shown = f"{status_names.get(status, f'status {status}')} ({status})"
        print(f"  {shown:<14} {count:5d}")
    if "Unmanaged" in have:
        print(f"Unmanaged among them: {sum(1 for n in kept if n['unmanaged'])}")

    per_site = Counter(v["site"] for v in view.values())
    ranked = sorted(per_site.items(), key=lambda kv: (-kv[1], kv[0]))
    site_label = {site: (scrub.label("Site", site) if scrub.enabled else f"site {site}") for site, _ in ranked}
    print(f"Sites with at least one Orion node: {len(per_site)}")
    for name, lo, hi in (("1 node", 1, 1), ("2-3 nodes", 2, 3), ("4-10 nodes", 4, 10), ("11+ nodes", 11, 10 ** 9)):
        print(f"  sites with {name:<10} {sum(1 for _, c in ranked if lo <= c <= hi):4d}")
    print("Largest sites by node count:")
    for site, count in ranked[:5]:
        print(f"  {site_label[site]:<18} {count:4d} nodes")

    # ---- 3. events -----------------------------------------------------
    heading("Event history (nodes only)")
    span = client.event_span()
    if span["total"]:
        age = (now - span["earliest"]).days
        print(f"Retention: Orion holds {span['total']} node events, oldest {span['earliest']:{STAMP}} "
              f"({age} days before server time), newest {span['latest']:{STAMP}}.")
        if age < args.days:
            print(f"  NOTE: the oldest event is newer than the {args.days}-day lookback, so history starts there.")
    else:
        print("Retention: Orion holds no node events at all.")
    type_names = best_effort(client.event_type_names, {})
    type_counts = client.event_type_counts(args.days)
    print(f"Event types in the last {args.days} days (all Orion nodes):")
    for etype, count in sorted(type_counts.items()):
        shown = f"{type_names.get(etype, f'type {etype}')} ({etype})"
        print(f"  {shown:<24} {count:6d}")

    events, event_errors = client.fetch_events(args.days)
    if event_errors:
        print(f"  {len(event_errors)} event row(s) could not be understood. First few:")
        for message in event_errors[:3]:
            print(f"    {message}")
    fetched = Counter(e["type"] for e in events)
    any_differs = False
    for etype in (op.EVENT_DOWN, op.EVENT_UP):
        in_orion, got = type_counts.get(etype, 0), fetched.get(etype, 0)
        differs = abs(in_orion - got) > max(2, in_orion // 100)
        any_differs = any_differs or differs
        flag = "   <-- DIFFERENT" if differs else ""
        print(f"  {type_names.get(etype, f'type {etype}')} ({etype}): Orion reports {in_orion}, fetched {got}{flag}")
    if client.last_event_duplicates:
        print(f"  WARNING: {client.last_event_duplicates} event(s) came back more than once and were dropped.")
    if any_differs:
        print("  Event queries actually sent:")
        for q in client.last_event_queries:
            print(f"    {q['clause']} -> {q['rows']} rows, event types received: {q['types_received']}")
    mine = [e for e in events if e["node_id"] in view]
    print(f"Down/Up events fetched: {len(events)}; for nodes that count: {len(mine)}")
    seq = op.analyze_event_sequences(mine)
    print(f"Nodes that count with Down/Up events: {seq['nodes']}")
    print(f"  nodes whose first event is an Up (already down when the window opened): {seq['first_is_up']}")
    print(f"  nodes whose first event is a Down: {seq['first_is_down']}")
    print(f"  Down right after a Down (missing Up): {seq['double_down']}")
    if seq["double_down"]:
        print(f"    spread over {seq['nodes_with_double_down']} node(s); the worst has {seq['worst_double_down']}; "
              f"median gap between the two Downs: {seq['median_double_down_gap']:.1f} min")
    print(f"  Up right after an Up (missing Down): {seq['double_up']}")
    print(f"  events for nodes Orion no longer lists: {sum(1 for e in events if e['node_id'] not in node_by_id)}")
    print(f"Nodes that count and are Down right now: {sum(1 for n in kept if n['status'] == op.STATUS_DOWN)}")

    # ---- 4. one site ---------------------------------------------------
    sample = sorted(view.values(), key=lambda v: v["node"]["node_id"])[:50]

    def node_name(v):
        if scrub.enabled:
            return scrub.label("Node", v["node"]["node_id"])
        return v["device"]["hostname"] if v["device"] else (v["node"]["caption"] or f"node {v['node']['node_id']}")

    if args.octet is not None:
        site = str(args.octet)
        site_view = sorted((v for v in view.values() if v["site"] == site), key=lambda v: v["node"]["node_id"])
        sample = site_view
        heading(f"One site: {scrub.label('Site', site) if scrub.enabled else f'site {site}'}")
        if not site_view:
            print("No node that counts belongs to that site.")
        else:
            for v in site_view:
                n = v["node"]
                tags = []
                if v["device"] and v["device"]["is_seed"]:
                    tags.append("seed")
                if v["method"] != "-":
                    tags.append(f"matched by {v['method']}")
                shown_ip = "" if scrub.enabled else f"  {n['ip'] or ''}"
                label = status_names.get(n["status"], f"status {n['status']}")
                print(f"  {node_name(v):<28} now {label:<10} [{', '.join(tags)}]{shown_ip}".rstrip())
            ids = {v["node"]["node_id"] for v in site_view}
            by_id = {v["node"]["node_id"]: v for v in site_view}
            site_events = sorted((e for e in events if e["node_id"] in ids), key=op.event_sort_key)
            print(f"\nDown/Up events ({len(site_events)}):")
            for e in site_events[:80]:
                kind = "DOWN" if e["type"] == op.EVENT_DOWN else "UP"
                print(f"  {e['time']:%Y-%m-%d %H:%M:%S}  {node_name(by_id[e['node_id']]):<28} {kind}")
            if len(site_events) > 80:
                print(f"  ... {len(site_events) - 80} more")
            print("\nRaw down intervals per node (no thresholds, no merging):")
            any_interval = False
            for v in site_view:
                for start, end in simple_intervals([e for e in site_events if e["node_id"] == v["node"]["node_id"]]):
                    any_interval = True
                    s = "before window" if start is None else f"{start:{STAMP}}"
                    t = "still down" if end is None else f"{end:{STAMP}}"
                    dur = "began before the window, length unknown" if start is None else fmt_duration((end or now) - start)
                    print(f"  {node_name(v):<28} {s} -> {t}   ({dur})")
            if not any_interval:
                print("  none in this window")

    # ---- 5. dump -------------------------------------------------------
    if args.dump:
        ids = [v["node"]["node_id"] for v in sample]
        ref = {nid: i + 1 for i, nid in enumerate(ids)}
        out_nodes = []
        for v in sample:
            n = v["node"]
            entry = {"node": ref[n["node_id"]], "status": n["status"], "unmanaged": n["unmanaged"],
                     "site": scrub.label("Site", v["site"]) if scrub.enabled else v["site"],
                     "seed": bool(v["device"] and v["device"]["is_seed"]), "match": v["method"]}
            if scrub.enabled:
                entry["name"] = scrub.label("Node", n["node_id"])
            else:
                entry.update({"name": node_name(v), "ip": n["ip"], "orion_caption": n["caption"], "node_id": n["node_id"]})
            out_nodes.append(entry)
        out_events = [{"node": ref[e["node_id"]], "time": e["time"].isoformat(), "type": e["type"]}
                      for e in sorted(events, key=op.event_sort_key) if e["node_id"] in ref]
        payload = {"note": "scrubbed sample" if scrub.enabled else "UNSCRUBBED sample - contains real names and addresses",
                   "server_now": now.isoformat(), "days": args.days, "nodes": out_nodes, "events": out_events}
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        suffix = "" if scrub.enabled else "  (NOT scrubbed)"
        print(f"\nWrote {len(out_nodes)} nodes and {len(out_events)} events to {args.dump}{suffix}")

    heading("Done")
    print("Read-only: nothing was changed in Orion or in SAD.")
    return 0


def parse_args(argv):
    cfg = _settings()
    p = argparse.ArgumentParser(description="Read-only probe of the Orion (SWIS) API for SAD.")
    p.add_argument("--host", help="Orion server name or IP (or set orion.host in settings.json)")
    p.add_argument("--port", type=int, default=int(cfg.get("port") or oc.DEFAULT_PORT))
    p.add_argument("--days", type=int, default=30, help="how many days of events to look at (default 30)")
    p.add_argument("--octet", help="show every SAD device and event for the site with this SAD site octet")
    p.add_argument("--sad-db", default=os.path.join(HERE, "sad.db"), help="SAD database to read (default: sad.db in the project root)")
    p.add_argument("--all-nodes", action="store_true", help="do not filter to SAD devices; use every Orion node")
    p.add_argument("--scrub", action="store_true", help="replace names, addresses and site numbers with labels")
    p.add_argument("--dump", metavar="FILE", help="save a small JSON sample of nodes and events")
    p.add_argument("--prompt", action="store_true", help="type the username and password instead of using the stored login")
    p.add_argument("--verify-tls", action="store_true", default=bool(cfg.get("verify_tls", False)),
                   help="check Orion's TLS certificate (off by default; most Orion servers are self-signed)")
    p.add_argument("--timeout", type=float, default=float(cfg.get("timeout_seconds") or 120))
    p.add_argument("--page-size", type=int, default=2000)
    p.add_argument("--http", action="store_true", help="plain HTTP instead of HTTPS (only for the offline test server)")
    return p.parse_args(argv)


def main(argv=None, credentials=None):
    args = parse_args(argv)
    host = args.host or _configured_host()
    if not host:
        print("ERROR: no Orion host. Pass --host NAME, or add \"orion\": {\"host\": \"NAME\"} to settings.json.", file=sys.stderr)
        return 1
    if args.days < 1:
        print("ERROR: --days must be at least 1.", file=sys.stderr)
        return 1
    user, password = credentials if credentials else get_credentials(args.prompt)
    client = oc.OrionClient(host, user, password, port=args.port, verify_tls=args.verify_tls, timeout=args.timeout,
                            scheme="http" if args.http else "https", page_size=args.page_size)
    try:
        return run(client, args, op.Scrubber(args.scrub), host)
    except oc.OrionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())