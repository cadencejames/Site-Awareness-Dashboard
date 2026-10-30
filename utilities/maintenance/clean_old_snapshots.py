"""One-time cleanup: re-clean stored config snapshots saved before the
parser learned to drop the '! NVRAM config last updated...' line.

  python utilities/maintenance/clean_old_snapshots.py                   dry run (changes nothing)
  python utilities/maintenance/clean_old_snapshots.py --apply           back up sad.db, then write
  python utilities/maintenance/clean_old_snapshots.py --apply --collapse
                                                                        also delete a snapshot that
                                                                        becomes identical to the one
                                                                        before it (never the baseline)
  python utilities/maintenance/clean_old_snapshots.py --db path\\to\\sad.db

Run it from the project root (or pass --db)."""
import argparse, hashlib, os, shutil, sqlite3, sys, time, zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))         # utilities/
import config_parser


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sad.db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--collapse", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"Cannot find {args.db}. Run this from the folder that holds sad.db, or use --db.")
    if args.apply:
        bak = f"{args.db}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(args.db, bak)
        print(f"Backup written: {bak}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, device_id, content_hash, config_z, nvram_updated, is_baseline "
        "FROM config_snapshots ORDER BY device_id, id").fetchall()

    changed = unchanged = skipped = 0
    for r in rows:
        text = zlib.decompress(r["config_z"]).decode("utf-8")
        res = config_parser.clean_config(text)
        if res["problem"] and "cut off" in res["problem"]:
            # NX-OS text has had its "!Command:" header stripped already, so it no longer
            # looks like NX-OS and has no trailing "end". Re-add the header and retry.
            res = config_parser.clean_config("!Command: show running-config\n" + text)
        if res["problem"]:
            skipped += 1
            print(f"  snapshot {r['id']} (device {r['device_id']}): left alone, {res['problem']}")
            continue
        new = res["text"]
        if new == text:
            unchanged += 1
            continue
        raw = new.encode("utf-8")
        conn.execute(
            "UPDATE config_snapshots SET config_z=?, content_hash=?, size_bytes=?, line_count=?, "
            "nvram_updated=COALESCE(nvram_updated, ?) WHERE id=?",
            (zlib.compress(raw, 9), hashlib.sha256(raw).hexdigest(), len(raw),
             new.count("\n"), res["nvram_updated"], r["id"]))
        changed += 1
        print(f"  snapshot {r['id']} (device {r['device_id']}): cleaned, "
              f"{text.count(chr(10)) - new.count(chr(10))} line(s) removed")

    # Snapshots that now match the one before them on the same device.
    dups = []
    prev_dev, prev_hash, prev_id = None, None, None
    for r in conn.execute(
            "SELECT id, device_id, content_hash, is_baseline FROM config_snapshots ORDER BY device_id, id"):
        if r["device_id"] == prev_dev and r["content_hash"] == prev_hash:
            dups.append((r["id"], prev_id, r["device_id"], r["is_baseline"]))
        prev_dev, prev_hash, prev_id = r["device_id"], r["content_hash"], r["id"]

    deleted = 0
    for sid, same_as, dev, is_base in dups:
        if args.collapse and not is_base:
            conn.execute("DELETE FROM config_snapshots WHERE id=?", (sid,))
            deleted += 1
            print(f"  snapshot {sid} (device {dev}): identical to {same_as} after cleaning, deleted")
        else:
            print(f"  snapshot {sid} (device {dev}): identical to {same_as} after cleaning"
                  + ("" if args.collapse else " (kept; use --collapse to delete)"))

    print(f"\n{changed} cleaned, {unchanged} already clean, {skipped} left alone, "
          f"{len(dups)} duplicate(s) found, {deleted} deleted.")
    if args.apply:
        conn.commit()
        print("Changes saved.")
    else:
        conn.rollback()
        print("Dry run only, nothing was changed. Add --apply to save.")


if __name__ == "__main__":
    main()