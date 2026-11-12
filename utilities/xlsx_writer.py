"""
xlsx_writer.py - writes a plain Excel workbook (.xlsx) using only the standard
library, so no openpyxl or xlsxwriter is needed.

    write_xlsx("report.xlsx", [
        {"name": "Summary", "rows": [["Site", "Availability %"], ["12", 99.123]],
         "widths": [10, 16], "header_rows": 1},
        ...
    ])

A sheet is a dict with:
  name         sheet name (up to 31 characters; characters Excel forbids are replaced)
  rows         list of rows; each row is a list of cell values
  widths       optional column widths in characters
  header_rows  how many leading rows are headers (bold, shaded, frozen); default 1
  filter       True to add an AutoFilter over the header row and data; default False

Cell values: None or "" leave the cell empty; bool, int and float are numbers
(bool becomes TRUE/FALSE); datetime and date become real Excel dates; anything else is
written as text. A value can also be a tuple (value, style) with style one of
"bold", "number" (2 decimals), "percent3" (0.000), "int" or "title" (large bold),
for a single cell that needs it. Text that starts with = + - @ is stored as plain
text, never as a formula.

A workbook written here opens in Excel, LibreOffice and openpyxl.
"""
import re
import zipfile
from datetime import date, datetime

_EPOCH = datetime(1899, 12, 30)
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")

# cellXfs indexes in the styles below
S_DEFAULT, S_HEADER, S_DATETIME, S_NUMBER, S_BOLD, S_PERCENT3, S_INT, S_TITLE, S_DATE = range(9)
_NAMED = {"bold": S_BOLD, "number": S_NUMBER, "percent3": S_PERCENT3, "int": S_INT, "title": S_TITLE}

STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="3"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd\\ hh:mm"/><numFmt numFmtId="165" formatCode="0.000"/><numFmt numFmtId="166" formatCode="yyyy\\-mm\\-dd"/></numFmts>
<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="14"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFD9E1F2"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left/><right/><top/><bottom style="thin"><color auto="1"/></bottom><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="9">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="1" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _esc(text):
    text = _ILLEGAL.sub("", str(text))
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def column_letter(index):
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _serial(value):
    if isinstance(value, datetime):
        return (value - _EPOCH).total_seconds() / 86400.0
    return float((datetime(value.year, value.month, value.day) - _EPOCH).days)


def _cell(ref, value, header):
    style = None
    if isinstance(value, tuple) and len(value) == 2:
        value, name = value
        style = _NAMED.get(name)
    if value is None or value == "":
        return ""
    if header:
        return f'<c r="{ref}" s="{S_HEADER}" t="inlineStr"><is><t xml:space="preserve">{_esc(value)}</t></is></c>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        s = f' s="{style}"' if style is not None else ""
        return f'<c r="{ref}"{s}><v>{value!r}</v></c>'
    if isinstance(value, (datetime, date)):
        s = S_DATETIME if isinstance(value, datetime) else S_DATE
        return f'<c r="{ref}" s="{s}"><v>{_serial(value)!r}</v></c>'
    s = f' s="{style}"' if style is not None else ""
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{_esc(value)}</t></is></c>'


def _sheet_xml(sheet):
    rows = sheet.get("rows") or []
    header_rows = sheet.get("header_rows", 1)
    widths = sheet.get("widths") or []
    width = max((len(r) for r in rows), default=1)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           f'<dimension ref="A1:{column_letter(max(width, 1) - 1)}{max(len(rows), 1)}"/>']
    if header_rows:
        out.append(f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="{header_rows}" topLeftCell="A{header_rows + 1}" '
                   f'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
    else:
        out.append('<sheetViews><sheetView workbookViewId="0"/></sheetViews>')
    out.append('<sheetFormatPr defaultRowHeight="15"/>')
    if widths:
        out.append("<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)) + "</cols>")
    out.append("<sheetData>")
    for r, row in enumerate(rows, start=1):
        header = r <= header_rows
        cells = "".join(_cell(f"{column_letter(c)}{r}", v, header) for c, v in enumerate(row))
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData>")
    if sheet.get("filter") and rows and header_rows:
        out.append(f'<autoFilter ref="A{header_rows}:{column_letter(max(width, 1) - 1)}{len(rows)}"/>')
    out.append("</worksheet>")
    return "".join(out)


def _clean_name(name, used):
    base = _BAD_SHEET_CHARS.sub("-", str(name or "Sheet")).strip("'") or "Sheet"
    base = base[:31]
    candidate, n = base, 2
    while candidate.lower() in used:
        suffix = f" ({n})"
        candidate = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(candidate.lower())
    return candidate


def write_xlsx(path, sheets):
    """Write the workbook. `sheets` is a list of sheet dicts (see the module docstring)."""
    if not sheets:
        raise ValueError("a workbook needs at least one sheet")
    used = set()
    names = [_clean_name(s.get("name"), used) for s in sheets]
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                     + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                               for i in range(1, len(sheets) + 1))
                     + "</Types>")
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                 "</Relationships>")
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                + "".join(f'<sheet name="{_esc(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, start=1))
                + "</sheets></workbook>")
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                         for i in range(1, len(sheets) + 1))
               + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               "</Relationships>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", STYLES_XML)
        for i, sheet in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(sheet))
    return names


def write_csv_files(directory, base_name, sheets):
    """Write one CSV per sheet (base_name - Sheet name.csv) and return their paths.
    Dates are written as 'YYYY-MM-DD HH:MM'. The files carry a byte order mark so Excel reads them as UTF-8."""
    import csv
    import os
    used, paths = set(), []
    for sheet in sheets:
        name = _clean_name(sheet.get("name"), used)
        path = os.path.join(directory, f"{base_name} - {name}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            for row in sheet.get("rows") or []:
                out = []
                for v in row:
                    if isinstance(v, tuple) and len(v) == 2:
                        v = v[0]
                    if isinstance(v, datetime):
                        v = v.strftime("%Y-%m-%d %H:%M")
                    elif isinstance(v, date):
                        v = v.strftime("%Y-%m-%d")
                    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
                        v = "'" + v          # stop Excel treating text as a formula
                    out.append("" if v is None else v)
                writer.writerow(out)
        paths.append(path)
    return paths