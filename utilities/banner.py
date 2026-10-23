"""Classification banner for the SAD static pages (dashboard and config viewer).

No dependencies beyond the standard library. See BANNER.md for how it is
wired into SAD and how to change the text or colors.

The text and any extra colors come from settings.json in the project root
(next to gui.py). The file is re-read whenever it changes, so editing it and
regenerating the dashboard is enough. No restart needed.

Use, in code that builds a page:
    import banner
    css  = "..." + banner.CSS                       # inside the <style>
    html = "<body>" + banner.html() + "..."         # right after <body>

Calling banner.set_text("...") yourself pins the text and ignores settings.json.
"""
import json
import os
from html import escape

# (text starts with, background, text color). First match wins, case ignored.
# Put longer or more specific names ABOVE shorter ones ("TOP SECRET" before
# "SECRET", "TS//SCI" before "TOP SECRET").
COLORS = [
    ("TOP SECRET//SCI", "#fce83a", "#000000"),
    ("TS//SCI", "#fce83a", "#000000"),
    ("TOP SECRET", "#ff8c00", "#000000"),
    ("SECRET", "#c8102e", "#ffffff"),
    ("CONFIDENTIAL", "#0033a0", "#ffffff"),
    ("CUI", "#502b85", "#ffffff"),
    ("UNCLASSIFIED", "#007a33", "#ffffff"),
]

# Used when the text matches nothing above.
DEFAULT_BG = "#555555"
DEFAULT_FG = "#ffffff"

# Shown when settings.json is missing a "classification" entry or cannot be read.
DEFAULT_TEXT = "UNCLASSIFIED"
ERROR_TEXT = "CLASSIFICATION SETTING UNREADABLE (check settings.json)"

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")

CSS = (
    ".class-banner{position:sticky;top:0;z-index:50;text-align:center;font-weight:700;"
    "letter-spacing:.08em;padding:3px 8px;font-size:13px;"
    "-webkit-print-color-adjust:exact;print-color-adjust:exact}"
)

_text = DEFAULT_TEXT
_extra = {}
_pinned = False        # True once set_text() is called: settings.json is then ignored
_loaded_stamp = "unset"  # last (mtime, size) of settings.json that was read, or None if absent


def set_text(text, extra=None):
    """Choose the banner text. `extra` maps a label to (background, text color)
    and wins over the built-in COLORS list. Pins the text: settings.json is
    ignored afterwards."""
    global _text, _extra, _pinned
    _pinned = True
    _text = str(text).strip()
    _extra = {}
    for label, pair in (extra or {}).items():
        _extra[str(label).upper()] = (pair[0], pair[1])


def _refresh():
    """Re-read settings.json if it changed since the last page. Never raises."""
    global _text, _extra, _loaded_stamp
    if _pinned:
        return
    try:
        st = os.stat(SETTINGS_PATH)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    if stamp == _loaded_stamp:
        return
    _loaded_stamp = stamp
    _text, _extra = DEFAULT_TEXT, {}
    if stamp is None:
        return
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("settings.json must hold a JSON object")
        _text = str(cfg.get("classification", DEFAULT_TEXT)).strip()
        for label, pair in (cfg.get("banner_styles") or {}).items():
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                _extra[str(label).upper()] = (str(pair[0]), str(pair[1]))
    except (OSError, ValueError) as e:
        # A broken file must not quietly fall back to a green UNCLASSIFIED banner.
        print(f"banner: could not read {SETTINGS_PATH}: {e}")
        _text, _extra = ERROR_TEXT, {}


def colors_for(text):
    """Return (background, text color) for a banner text."""
    upper = text.upper()
    for label, pair in _extra.items():
        if upper.startswith(label):
            return pair
    for label, bg, fg in COLORS:
        if upper.startswith(label):
            return bg, fg
    return DEFAULT_BG, DEFAULT_FG


def html(text=None):
    """The banner element, or "" when the text is blank (banner turned off)."""
    _refresh()
    text = _text if text is None else str(text).strip()
    if not text:
        return ""
    bg, fg = colors_for(text)
    return (f'<div class="class-banner" style="background:{escape(bg, quote=True)};color:{escape(fg, quote=True)}">'
            f"{escape(text)}</div>")