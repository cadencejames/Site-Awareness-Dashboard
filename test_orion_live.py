#!/usr/bin/env python3
"""
test_orion_live.py - read-only probe of a live SolarWinds Orion server.

Run it from the SAD project root on the machine that can reach Orion:

    python test_orion_live.py --host orion.example.local --days 30 --scrub

It signs in with the stored 'tacacs' login (the same one SAD uses for devices;
use --prompt to type a username and password instead), asks Orion a handful of
read-only questions, and prints a report. Nothing is written to Orion or to
SAD's database, and the password is never printed or saved.

What it reports:
  - Orion's clock, and which node properties this Orion version offers
  - how many nodes there are, their statuses, and how they spread across the
    second octet of their IP address (the site number)
  - how far back Orion's event history goes (retention), which event types it
    records, and whether Down/Up events pair up cleanly
  - with --octet N: every node and Down/Up event for one site, so you can
    compare against Orion's own console or what is known to have happened

Sharing the output: add --scrub and node names, IP addresses and site numbers
are replaced with Node-1, Site-2, ... Counts, statuses, event types and times
are kept. --dump FILE saves a small sample (one site with --octet, otherwise
the first 50 nodes) as JSON that can serve as realistic test data; with
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


def simple_intervals(node_events, now):
    """Raw down intervals for one node, no thresholds: [(start|None, end|None)]."""
    out, start, down = [], None, False
    for e in sorted(node_events, key=op.event_sort_key):
        if e["type"] == op.EVENT_DOWN and not down:
            start, down = e["time"], True
        elif e["type"] == op.EVENT_UP:
            if down:
                out.append((start, e["time"]))
            else:
                out.append((None, e["time"]))    # was already down when the window opened
            down = False
    if down:
        out.append((start, None))
    return out


def run(client, args, scrub, host):
    # ---- 1. connection ------------------------------------------------
    heading("Connection")
    now = client.server_now()
    shown_host = "<orion host>" if scrub.enabled else host
    print(f"Connected to {shown_host}.")
    print(f"Orion server time: {now:%Y-%m-%d %H:%M:%S}   (this machine: {datetime.now():%Y-%m-%d %H:%M:%S})")
    print("Event times below are on the Orion server's clock.")

    # ---- 2. node properties --------------------------------------------
    have, missing = client.node_fields()
    heading("Node properties")
    print(f"Available: {', '.join(have)}")
    missing_text = ", ".join(missing) if missing else "none"
    print(f"Not on this Orion version: {missing_text}")

    # ---- 3. nodes ------------------------------------------------------
    nodes, node_errors = client.fetch_nodes()
    status_names = best_effort(client.status_names, {})
    heading("Nodes")
    print(f"Nodes: {len(nodes)}")
    if node_errors:
        print(f"  {len(node_errors)} row(s) could not be understood. First few:")
        for message in node_errors[:3]:
            print(f"    {message}")
    if not nodes:
        print("  WARNING: no node could be read, so the site breakdown below is empty and means nothing.")
    counts = Counter(n["status"] for n in nodes)
    for status, count in sorted(counts.items(), key=lambda kv: (kv[0] is None, kv[0])):
        label = status_names.get(status, f"status {status}")
        shown = f"{label} ({status})"
        print(f"  {shown:<14} {count:5d}")
    unmanaged = sum(1 for n in nodes if n["unmanaged"])
    if "Unmanaged" in have:
        print(f"Unmanaged nodes: {unmanaged}")
    with_ip = [n for n in nodes if n["ip"]]
    print(f"Nodes with a usable IPv4 address: {len(with_ip)}   without: {len(nodes) - len(with_ip)}")

    firsts = Counter(int(n["ip"].split(".")[0]) for n in with_ip)
    firsts_text = ", ".join(f"{octet} ({count} nodes)" for octet, count in sorted(firsts.items()))
    print(f"First octets in use: {firsts_text}")
    by_octet = Counter(op.second_octet(n["ip"]) for n in with_ip)
    ranked = sorted(by_octet.items(), key=lambda kv: (-kv[1], kv[0]))
    site_label = {octet: (scrub.label("Site", octet) if scrub.enabled else f"second octet {octet}") for octet, _ in ranked}
    print(f"Distinct second octets (site numbers): {len(by_octet)}")
    buckets = [("1 node", 1, 1), ("2-3 nodes", 2, 3), ("4-10 nodes", 4, 10), ("11+ nodes", 11, 10 ** 9)]
    for name, lo, hi in buckets:
        n_sites = sum(1 for _, c in ranked if lo <= c <= hi)
        print(f"  sites with {name:<10} {n_sites:4d}")
    print("Largest sites by node count:")
    for octet, count in ranked[:5]:
        print(f"  {site_label[octet]:<18} {count:4d} nodes")

    # ---- 4. events -----------------------------------------------------
    heading("Event history (nodes only)")
    span = client.event_span()
    if span["total"]:
        age = (now - span["earliest"]).days
        oldest, newest = span["earliest"].strftime(STAMP), span["latest"].strftime(STAMP)
        print(f"Retention: Orion holds {span['total']} node events, oldest {oldest} ({age} days before server time), newest {newest}.")
        if age < args.days:
            print(f"  NOTE: the oldest event is newer than the {args.days}-day lookback, so history starts there.")
    else:
        print("Retention: Orion holds no node events at all.")
    type_names = best_effort(client.event_type_names, {})
    type_counts = client.event_type_counts(args.days)
    print(f"Event types in the last {args.days} days:")
    for etype, count in sorted(type_counts.items()):
        type_label = type_names.get(etype, f"type {etype}")
        shown = f"{type_label} ({etype})"
        print(f"  {shown:<24} {count:6d}")
    others = sorted(t for t in type_counts if t not in (op.EVENT_DOWN, op.EVENT_UP))
    if others:
        other_text = ", ".join(type_names.get(t, f"type {t}") for t in others)
        print(f"Other event types are present ({other_text}). They are not replayed as Down/Up; check whether any of them mean 'unreachable'.")

    events, event_errors = client.fetch_events(args.days)
    print(f"Down/Up events fetched: {len(events)}")
    if event_errors:
        print(f"  {len(event_errors)} event row(s) could not be understood. First few:")
        for message in event_errors[:3]:
            print(f"    {message}")
    fetched_by_type = Counter(e["type"] for e in events)
    suspicious = False
    for etype in (op.EVENT_DOWN, op.EVENT_UP):
        in_orion, got = type_counts.get(etype, 0), fetched_by_type.get(etype, 0)
        flag = "" if in_orion == got else "   <-- DIFFERENT"
        suspicious = suspicious or in_orion != got
        print(f"  {type_names.get(etype, f'type {etype}')} ({etype}): Orion reports {in_orion}, fetched {got}{flag}")
    if suspicious:
        print("  The counts above should match. Raw look at the newest events of each type, as Orion sent them:")
        for etype in (op.EVENT_DOWN, op.EVENT_UP):
            samples = best_effort(lambda: client.sample_events(etype), None)
            if samples is None:
                print(f"    type {etype}: the sample query failed")
                continue
            for row in samples:
                parts = [f"{key}={row[key]!r} ({type(row[key]).__name__})" for key in sorted(row)]
                print(f"    type {etype}: " + "; ".join(parts))
    known = {n["node_id"] for n in nodes}
    orphans = sum(1 for e in events if e["node_id"] not in known)
    seq = op.analyze_event_sequences(events)
    print(f"Nodes with Down/Up events: {seq['nodes']}")
    print(f"  nodes whose first event is an Up (already down when the window opened): {seq['first_is_up']}")
    print(f"  nodes whose first event is a Down: {seq['first_is_down']}")
    print(f"  Down right after a Down (missing Up): {seq['double_down']}")
    if seq["double_down"]:
        print(f"    spread over {seq['nodes_with_double_down']} node(s); the worst has {seq['worst_double_down']}; "
              f"median gap between the two Downs: {seq['median_double_down_gap']:.1f} min")
    print(f"  Up right after an Up (missing Down): {seq['double_up']}")
    print(f"  events for nodes no longer in Orion: {orphans}")
    down_now = sum(1 for n in nodes if n["status"] == op.STATUS_DOWN)
    print(f"Nodes currently Down: {down_now}")

    # ---- 5. one site ---------------------------------------------------
    sample_nodes = nodes[:50]
    if args.octet is not None:
        site_nodes = sorted((n for n in nodes if op.second_octet(n["ip"]) == args.octet), key=lambda n: n["node_id"])
        sample_nodes = site_nodes
        name = scrub.label("Site", args.octet) if scrub.enabled else f"second octet {args.octet}"
        heading(f"One site: {name}")
        if not site_nodes:
            print("No node has that second octet.")
        else:
            def node_name(n):
                return scrub.label("Node", n["node_id"]) if scrub.enabled else (n["caption"] or f"node {n['node_id']}")
            for n in site_nodes:
                label = status_names.get(n["status"], f"status {n['status']}")
                shown_ip = "" if scrub.enabled else "  " + (n["ip"] or "")
                print(f"  {node_name(n):<28} now {label:<12}{shown_ip}".rstrip())
            ids = {n["node_id"] for n in site_nodes}
            by_id = {n["node_id"]: n for n in site_nodes}
            site_events = sorted((e for e in events if e["node_id"] in ids), key=op.event_sort_key)
            print(f"\nDown/Up events ({len(site_events)}):")
            for e in site_events[:80]:
                kind = "DOWN" if e["type"] == op.EVENT_DOWN else "UP"
                print(f"  {e['time']:%Y-%m-%d %H:%M:%S}  {node_name(by_id[e['node_id']]):<28} {kind}")
            if len(site_events) > 80:
                print(f"  ... {len(site_events) - 80} more")
            print("\nRaw down intervals per node (no thresholds, no merging):")
            any_interval = False
            for n in site_nodes:
                for start, end in simple_intervals([e for e in site_events if e["node_id"] == n["node_id"]], now):
                    any_interval = True
                    s = "before window" if start is None else start.strftime(STAMP)
                    t = "still down" if end is None else end.strftime(STAMP)
                    if start is None:
                        dur = "began before the window, length unknown"
                    else:
                        dur = fmt_duration((end or now) - start)
                    print(f"  {node_name(n):<28} {s} -> {t}   ({dur})")
            if not any_interval:
                print("  none in this window")

    # ---- 6. dump -------------------------------------------------------
    if args.dump:
        ids = {n["node_id"] for n in sample_nodes}
        node_order = sorted(ids)
        ref = {nid: i + 1 for i, nid in enumerate(node_order)}
        by_id = {n["node_id"]: n for n in nodes}
        out_nodes = []
        for nid in node_order:
            n = by_id[nid]
            entry = {"node": ref[nid], "status": n["status"], "unmanaged": n["unmanaged"]}
            entry["site"] = scrub.label("Site", op.second_octet(n["ip"])) if scrub.enabled else op.second_octet(n["ip"])
            if scrub.enabled:
                entry["name"] = scrub.label("Node", nid)
            else:
                entry.update({"name": n["caption"], "ip": n["ip"], "dns": n["dns"], "node_id": nid})
            out_nodes.append(entry)
        out_events = [{"node": ref[e["node_id"]], "time": e["time"].isoformat(), "type": e["type"]}
                      for e in sorted(events, key=op.event_sort_key) if e["node_id"] in ids]
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
    p.add_argument("--octet", type=int, help="show every node and event for the site with this second octet")
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