# Classification banner in SAD

Every page SAD generates (index, site pages, Site Map, Search Clients, Stale
Devices, Exports, and the Device Configs viewer) starts with a colored
classification banner. It is one module, `utilities/banner.py`, and it needs
nothing installed.

## Change the text or color

Edit `settings.json` in the project root (next to `gui.py`):

    {
      "classification": "CUI",
      "banner_styles": {}
    }

Then regenerate the dashboard. No restart is needed, the file is re-read
whenever it changes. Pages you do not regenerate keep their old banner, so
run a full dashboard generation after changing it.

The banner color comes from the text. `banner.py` has a COLORS list near the
top. Each row is: text it starts with, background, text color.

    ("WORK ITEMS", "#f4a6c8", "#000000"),     <- pink, black text

Rules:
- Matching ignores upper and lower case, and only needs the start of the text.
  "UNCLASSIFIED//FOUO" matches the UNCLASSIFIED row.
- First match wins. Keep longer names above shorter ones ("TOP SECRET" above
  "SECRET").
- Text that matches no row is dark gray with white text (DEFAULT_BG/DEFAULT_FG).
- Blank text turns the banner off.

## Add a label without editing banner.py

Add it to `banner_styles` in `settings.json` as a pair of colors:

    {
      "classification": "Lab Notes",
      "banner_styles": {"Lab Notes": ["#aaddff", "#000000"]}
    }

Colors are any CSS color: "#rrggbb" or a name like "pink". Pick a text color
that is readable on the background (black on light colors, white on dark ones).
An entry that is not a two-item list is ignored.

## If settings.json is missing or broken

- File missing, or no "classification" entry: the banner says UNCLASSIFIED
  (green). Change `DEFAULT_TEXT` in `banner.py` if you would rather default to
  something else, or to blank.
- File present but not valid JSON: the banner says "CLASSIFICATION SETTING
  UNREADABLE (check settings.json)" in gray, and the generator prints the
  parse error. It never falls back to UNCLASSIFIED on a broken file, so a typo
  cannot make a page look cleared.

## How it is wired in

- `dashboard_generate.py`: each of the six page templates has `{banner.CSS}`
  at the end of its `<style>` block and `{banner.html()}` right after `<body>`.
  A new page built the same way needs those same two additions.
- `config_viewer.py` and `config_viewer_template.html`: the template has the
  placeholders `__BANNER_CSS__` (end of the `<style>` block) and `__BANNER__`
  (right after `<body>`), filled in when `configs.html` is written.

## What it does

- Sticks to the top of the window while scrolling.
- Same colors in light and dark mode.
- Prints in color (the print rule forces background colors on).
- Escapes the text and the colors, so characters like & and < are safe.

## Matching colors used here

| Marking | Background | Text |
|---|---|---|
| UNCLASSIFIED | #007a33 green | white |
| CUI | #502b85 purple | white |
| CONFIDENTIAL | #0033a0 blue | white |
| SECRET | #c8102e red | white |
| TOP SECRET | #ff8c00 orange | black |
| TS//SCI | #fce83a yellow | black |