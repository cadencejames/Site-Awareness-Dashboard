"""
config_viewer.py - builds the static config viewer (dashboard/configs.html
plus dashboard/configs/*.js data files) from the config_snapshots table.

Secrets are redacted HERE, while the data files are written (see
config_parser.redact_config); the stored text in sad.db is never changed
and no raw secret ever lands in the dashboard folder.

Data files are plain .js (loaded with script tags) so the page works
opened straight from a file share as well as from a web server. Each
device with snapshots gets its own dev_<id>.js; a manifest of what was
written lets a nightly regeneration skip every device whose snapshots
have not changed.

Usage (from the project root, like the other generators):
    python3 config_viewer.py              # regenerate everything
The GUI's "Generate dashboard" action calls generate_config_pages() too.
"""
import datetime
import json
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "utilities"))

import db  # noqa: E402
import config_parser  # noqa: E402
import banner  # noqa: E402

OUTPUT_DIR = os.path.join(HERE, "dashboard")
DATA_DIRNAME = "configs"
TEMPLATE_PATH = os.path.join(HERE, "config_viewer_template.html")

# Same palette as dashboard_generate.CSS_TOKENS (kept in step by hand so
# this module does not have to import the dashboard generator).
CSS_TOKENS = """
:root{
  --bg: #0b0f14; --panel: #121821; --panel-2: #182130; --border: #24303f;
  --text: #dbe4ee; --text-dim: #7c8a9a; --accent: #e8a33d; --accent-dim: #7a5a26;
  --up: #3ddc84; --warn: #e85d5d; --radius: 10px;
  --mono: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  --sans: 'Segoe UI', Tahoma, Arial, sans-serif;
}
html[data-theme="light"]{
  --bg: #f2f4f7; --panel: #ffffff; --panel-2: #eef1f5; --border: #dbe1e8;
  --text: #1b2531; --text-dim: #6b7684; --accent: #c07a1e; --accent-dim: #f0d6ac;
  --up: #1f9d5c; --warn: #c0392b;
}
"""


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _dump(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def generate_config_pages(conn, site_id: int = None, out_dir: str = None) -> dict:
    """Write configs.html, configs/index.js and one configs/dev_<id>.js per
    device that has snapshots. With site_id, only that site's device files
    are (re)built - the index and page are always rewritten, they are
    small. Returns {"devices", "written", "skipped", "redacted_lines"}."""
    out_dir = out_dir or OUTPUT_DIR
    data_dir = os.path.join(out_dir, DATA_DIRNAME)
    os.makedirs(data_dir, exist_ok=True)

    manifest_path = os.path.join(data_dir, "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}

    devices = conn.execute(
        """
        SELECT d.id, d.hostname, d.mgmt_ip, d.platform, d.serial_number, d.site_id, s.site_octet, s.site_name,
               agg.n, agg.latest_id, agg.oldest_id,
               snap.collected_at AS latest_at, snap.changed_at AS latest_changed_at, snap.changed_by AS latest_changed_by,
               st.last_attempt_at, st.last_success_at, st.last_error
        FROM devices d
        JOIN sites s ON s.id = d.site_id
        LEFT JOIN (SELECT device_id, COUNT(*) AS n, MAX(id) AS latest_id, MIN(id) AS oldest_id
                   FROM config_snapshots GROUP BY device_id) agg ON agg.device_id = d.id
        LEFT JOIN config_snapshots snap ON snap.id = agg.latest_id
        LEFT JOIN config_pull_status st ON st.device_id = d.id
        WHERE d.deleted_at IS NULL AND s.deleted_at IS NULL
          AND (agg.n IS NOT NULL OR st.device_id IS NOT NULL)
        ORDER BY d.hostname
        """
    ).fetchall()

    index, live_ids = [], set()
    stats = {"devices": len(devices), "written": 0, "skipped": 0, "redacted_lines": 0}
    for d in devices:
        n = d["n"] or 0
        live_ids.add(str(d["id"]))
        index.append({
            "id": d["id"], "h": d["hostname"], "s": d["site_octet"], "sn": d["site_name"] or "",
            "ip": d["mgmt_ip"] or "", "pl": d["platform"] or "", "se": d["serial_number"] or "",
            "n": n, "c": d["latest_at"], "ca": d["latest_changed_at"], "cb": d["latest_changed_by"],
            "ok": d["last_success_at"], "ea": d["last_attempt_at"], "er": d["last_error"],
        })
        if not n:
            continue
        dev_path = os.path.join(data_dir, f"dev_{d['id']}.js")
        # A one-site run leaves other sites' existing files alone, but still builds any that
        # are missing so the device index never points at a file that does not exist.
        if site_id is not None and d["site_id"] != site_id and os.path.exists(dev_path):
            continue
        sig = f"{config_parser.REDACTION_VERSION}:{d['latest_id']}:{d['oldest_id']}:{n}"
        if manifest.get(str(d["id"])) == sig and os.path.exists(dev_path):
            stats["skipped"] += 1
            continue
        snaps = []
        for r in conn.execute(
            """SELECT id, collected_at, changed_at, changed_by, nvram_updated, is_baseline, size_bytes, config_z
               FROM config_snapshots WHERE device_id = ? ORDER BY id DESC""", (d["id"],),
        ).fetchall():
            text, redacted = config_parser.redact_config(zlib.decompress(r["config_z"]).decode("utf-8"))
            stats["redacted_lines"] += redacted
            snaps.append({
                "id": r["id"], "at": r["collected_at"], "ca": r["changed_at"], "cb": r["changed_by"],
                "nv": r["nvram_updated"], "bl": r["is_baseline"], "sz": r["size_bytes"], "t": text,
            })
        _write_atomic(dev_path, f"(window.CFG_DATA=window.CFG_DATA||{{}})[{d['id']}]={_dump(snaps)};\n")
        manifest[str(d["id"])] = sig
        stats["written"] += 1

    if site_id is None:  # full run: drop data for devices that no longer qualify
        for name in os.listdir(data_dir):
            if name.startswith("dev_") and name.endswith(".js") and name[4:-3] not in live_ids:
                os.remove(os.path.join(data_dir, name))
        manifest = {k: v for k, v in manifest.items() if k in live_ids}
    _write_atomic(manifest_path, _dump(manifest))

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _write_atomic(os.path.join(data_dir, "index.js"),
                  f"window.CFG_GENERATED={_dump(stamp)};\nwindow.CFG_INDEX={_dump(index)};\n")
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        page = f.read().replace("__CSS_TOKENS__", CSS_TOKENS).replace("__BANNER_CSS__", banner.CSS).replace("__BANNER__", banner.html())
    _write_atomic(os.path.join(out_dir, "configs.html"), page)
    return stats


if __name__ == "__main__":
    with db.get_conn() as conn:
        s = generate_config_pages(conn)
    print(f"  Wrote {os.path.join(OUTPUT_DIR, 'configs.html')} - {s['devices']} device(s): "
          f"{s['written']} rebuilt, {s['skipped']} unchanged, {s['redacted_lines']} secret line(s) redacted.")