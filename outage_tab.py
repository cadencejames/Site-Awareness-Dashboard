"""
outage_tab.py - the "Outages" tab for gui.py.

Top row     last pull and whether it is getting old, "Pull from Orion", the
            month to look at, "Export month...".
Left table  every site for that month: tier, outage count, minutes down and
            both availability numbers (all downtime / planned excluded).
Tier line   the average for each tier group, same figures as the Excel
            "By tier" sheet.
Bottom      the selected site's outages (and its other devices' outages).

All numbers come from outage_report.compute_month, the same code the
command line and the Excel export use. A pull runs in a background thread
(the login is the stored 'tacacs' one) and is stored through the write queue.
"""
import datetime
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "utilities"))

import db  # noqa: E402
import orion_client as oc  # noqa: E402
import outage_calc  # noqa: E402
import outage_db  # noqa: E402
import outage_report as rpt  # noqa: E402
import settings_loader  # noqa: E402
import write_queue  # noqa: E402
import xlsx_writer  # noqa: E402
import pull_orion  # noqa: E402

STAMP = "%Y-%m-%d %H:%M"
TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


def _when(value):
    if value is None:
        return ""
    return value.strftime(STAMP) if hasattr(value, "strftime") else str(value)


def _pct(value):
    return "" if value is None else f"{value:.3f}%"


def _mins(value):
    return "" if value is None else f"{value:.0f}"


def _parse_when(text, label):
    text = (text or "").strip()
    for fmt in TIME_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f'{label} must look like "2026-11-20 22:00" (Orion time), not "{text}"')


def _sites_text(sites):
    if sites is None:
        return "All sites"
    if len(sites) > 6:
        return ", ".join(sites[:6]) + f" (+{len(sites) - 6})"
    return ", ".join(sites)


class WindowDialog(tk.Toplevel):
    """Small form for a planned window. on_save(values) returns None when it saved,
    or a plain-text problem to show (the form then stays open)."""

    def __init__(self, parent, title, on_save, sites="", all_sites=False, start="", end="",
                 reason="", sites_locked=False, note=""):
        super().__init__(parent)
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self._on_save = on_save
        body = ttk.Frame(self, padding=12)
        body.grid(row=0, column=0)
        if note:
            ttk.Label(body, text=note, wraplength=420, justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.sites_var = tk.StringVar(value=sites)
        self.all_var = tk.BooleanVar(value=all_sites)
        self.start_var = tk.StringVar(value=start)
        self.end_var = tk.StringVar(value=end)
        self.reason_var = tk.StringVar(value=reason)
        ttk.Label(body, text="Sites (site numbers, comma separated):").grid(row=1, column=0, sticky="w")
        self.sites_entry = ttk.Entry(body, textvariable=self.sites_var, width=34)
        self.sites_entry.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self.all_check = ttk.Checkbutton(body, text="All sites", variable=self.all_var, command=self._sync)
        self.all_check.grid(row=2, column=1, sticky="w", padx=(8, 0))
        ttk.Label(body, text="Start (Orion time):").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(body, textvariable=self.start_var, width=34).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(body, text="End (Orion time):").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(body, textvariable=self.end_var, width=34).grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(body, text="Reason (optional):").grid(row=5, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(body, textvariable=self.reason_var, width=34).grid(row=5, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(body, text="Times look like 2026-11-20 22:00", foreground="#555555").grid(row=6, column=1, sticky="w", padx=(8, 0))
        self.message = ttk.Label(body, text="", foreground="#c62828", wraplength=420, justify="left")
        self.message.grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Save", command=self._save).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=(6, 0))
        if sites_locked:
            self.sites_entry.configure(state="disabled")
            self.all_check.configure(state="disabled")
        else:
            self._sync()
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.focus_set()

    def _sync(self):
        self.sites_entry.configure(state="disabled" if self.all_var.get() else "normal")

    def _save(self):
        values = {"sites": None if self.all_var.get() else self.sites_var.get(), "start": self.start_var.get(),
                  "end": self.end_var.get(), "reason": self.reason_var.get()}
        problem = self._on_save(values)
        if problem:
            self.message.configure(text=problem)
        else:
            self.destroy()


TIER_CHOICES = ("No tier", "Tier 0 - most important", "Tier 1", "Tier 2", "Tier 3 - least important",
                "Tier 4 - offline often (own operations)")


def _tier_from_choice(text):
    """Combobox text -> None or 0-4."""
    return None if text == TIER_CHOICES[0] else TIER_CHOICES.index(text) - 1


def _tier_label(tier):
    return "-" if tier is None else str(tier)


def _save_tiers(site_ids, tier):
    """Check, then queue the change. Returns None when saved, else a plain-text problem."""
    try:
        tier = outage_db.validate_tier(tier)
    except ValueError as e:
        return str(e)
    try:
        if len(site_ids) == 1:
            write_queue.queue_and_wait("set_site_tier", timeout=30, site_id=int(site_ids[0]), tier=tier)
        else:
            write_queue.queue_and_wait("set_site_tiers", timeout=30, site_ids=[int(i) for i in site_ids], tier=tier)
    except (RuntimeError, TimeoutError, ValueError) as e:
        return f"Saving failed: {str(e)[:200]}"
    return None


class TierControls(ttk.Frame):
    """A tier dropdown for the Site manager's Actions area. Choosing a value saves it
    straight away. Also holds the button that opens the several-sites dialog.
    The Site manager calls set_site(site) when a site is picked and set_enabled(False)
    when none is."""

    def __init__(self, parent, on_changed=None):
        super().__init__(parent)
        self._on_changed = on_changed
        self._site = None
        self._quiet = False
        ttk.Label(self, text="Tier:").grid(row=0, column=0, sticky="w")
        self.var = tk.StringVar(value=TIER_CHOICES[0])
        self.box = ttk.Combobox(self, textvariable=self.var, values=TIER_CHOICES, state="disabled", width=34)
        self.box.grid(row=0, column=1, sticky="w", padx=(6, 10))
        self.box.bind("<<ComboboxSelected>>", self._on_pick)
        self.bulk_button = ttk.Button(self, text="Set tier for several sites...", command=self._open_bulk)
        self.bulk_button.grid(row=0, column=2, sticky="w")

    def set_site(self, site):
        self._site = site
        usable = site is not None and site["site_octet"] != db.UNASSIGNED_SITE_OCTET
        self._quiet = True
        self.var.set(TIER_CHOICES[0] if site is None or site["tier"] is None else TIER_CHOICES[int(site["tier"]) + 1])
        self._quiet = False
        self.box.configure(state="readonly" if usable else "disabled")

    def set_enabled(self, enabled):
        if not enabled:
            self._site = None
            self.box.configure(state="disabled")

    def _on_pick(self, event=None):
        if self._quiet or self._site is None:
            return
        tier = _tier_from_choice(self.var.get())
        if tier == self._site["tier"]:
            return
        problem = _save_tiers([self._site["id"]], tier)
        if problem:
            messagebox.showerror("Set tier", problem)
            self.set_site(self._site)
            return
        if self._on_changed:
            self._on_changed()

    def _open_bulk(self):
        BulkTierDialog(self, self._on_changed)


class BulkTierDialog(tk.Toplevel):
    """Pick several sites and give them all one tier (or none)."""

    def __init__(self, parent, on_changed=None):
        super().__init__(parent)
        self.title("Set tier for several sites")
        self.transient(parent.winfo_toplevel())
        self._on_changed = on_changed
        self._sites = []          # every site: dict(id, octet, name, tier)
        self._shown = []          # the ones passing the filter, in list order
        self._selected = set()    # ids picked, kept while the filter changes
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=(10, 10, 10, 0))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Filter:").grid(row=0, column=0, sticky="w")
        self.filter_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.filter_var)
        entry.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.filter_var.trace_add("write", lambda *a: self._fill())

        mid = ttk.Frame(self, padding=(10, 6, 10, 0))
        mid.grid(row=1, column=0, sticky="nsew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(mid, selectmode="extended", height=16, width=56, exportselection=False,
                                  font=("Consolas", 10))
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(mid, orient="vertical", command=self.listbox.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        picks = ttk.Frame(self, padding=(10, 6, 10, 0))
        picks.grid(row=2, column=0, sticky="ew")
        ttk.Button(picks, text="Select all shown", command=self._select_shown).pack(side="left")
        ttk.Button(picks, text="Select shown with no tier", command=self._select_untiered).pack(side="left", padx=(6, 0))
        ttk.Button(picks, text="Clear selection", command=self._clear).pack(side="left", padx=(6, 0))
        self.count_label = ttk.Label(picks, text="")
        self.count_label.pack(side="right")

        bottom = ttk.Frame(self, padding=10)
        bottom.grid(row=3, column=0, sticky="ew")
        ttk.Label(bottom, text="Give them:").pack(side="left")
        self.tier_var = tk.StringVar(value=TIER_CHOICES[2])
        ttk.Combobox(bottom, textvariable=self.tier_var, values=TIER_CHOICES, state="readonly", width=34).pack(side="left", padx=(6, 10))
        self.apply_button = ttk.Button(bottom, text="Apply", command=self._apply)
        self.apply_button.pack(side="left")
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right")
        self.message = ttk.Label(self, text="", padding=(10, 0, 10, 8), foreground="#1b5e20")
        self.message.grid(row=4, column=0, sticky="w")

        self._load()
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        entry.focus_set()

    def _load(self):
        with db.get_conn() as conn:
            rows = db.get_all_sites(conn, include_unassigned=False)
        self._sites = [{"id": r["id"], "octet": r["site_octet"], "name": r["site_name"] or "(unset)", "tier": r["tier"]}
                       for r in rows]
        self._fill()

    def _fill(self):
        needle = self.filter_var.get().strip().lower()
        self._shown = [s for s in self._sites if not needle or needle in s["octet"].lower() or needle in s["name"].lower()]
        self.listbox.delete(0, "end")
        for s in self._shown:
            self.listbox.insert("end", f"{s['octet']:>8}  {s['name'][:30]:<30}  tier {_tier_label(s['tier'])}")
        for i, s in enumerate(self._shown):
            if s["id"] in self._selected:
                self.listbox.selection_set(i)
        self._update_count()

    def _on_select(self, event=None):
        picked = set(self.listbox.curselection())
        for i, s in enumerate(self._shown):
            if i in picked:
                self._selected.add(s["id"])
            else:
                self._selected.discard(s["id"])
        self._update_count()

    def _update_count(self):
        self.count_label.configure(text=f"{len(self._selected)} selected, {len(self._shown)} shown")

    def _select_shown(self):
        self.listbox.selection_set(0, "end")
        self._on_select()

    def _select_untiered(self):
        self.listbox.selection_clear(0, "end")
        for i, s in enumerate(self._shown):
            if s["tier"] is None:
                self.listbox.selection_set(i)
        self._on_select()

    def _clear(self):
        self.listbox.selection_clear(0, "end")
        self._selected.clear()
        self._update_count()

    def _apply(self):
        ids = sorted(self._selected)
        if not ids:
            self.message.configure(text="Pick at least one site first.", foreground="#c62828")
            return
        tier = _tier_from_choice(self.tier_var.get())
        what = "no tier" if tier is None else f"tier {tier}"
        if not messagebox.askyesno("Set tier", f"Set {len(ids)} site(s) to {what}?", parent=self):
            return
        problem = _save_tiers(ids, tier)
        if problem:
            self.message.configure(text=problem, foreground="#c62828")
            return
        self.message.configure(text=f"{len(ids)} site(s) set to {what}.", foreground="#1b5e20")
        self._selected.clear()
        self._load()
        if self._on_changed:
            self._on_changed()


class OutageTab(ttk.Frame):
    def __init__(self, parent, creds):
        super().__init__(parent)
        self._creds = creds
        self._queue = queue.Queue()
        self._busy = False
        self._report = None
        self._month_by_text = {}
        self._detail_rows = {}
        self._planned_rows = {}
        self._build_layout()
        self.refresh()
        self._poll()

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------

    def _build_layout(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=3)
        self.rowconfigure(4, weight=2)

        top = ttk.Frame(self, padding=(10, 10, 10, 0))
        top.grid(row=0, column=0, sticky="ew")
        self.pull_button = ttk.Button(top, text="Pull from Orion", command=self._on_pull)
        self.pull_button.pack(side="left")
        ttk.Label(top, text="Month:").pack(side="left", padx=(14, 4))
        self.month_var = tk.StringVar()
        self.month_box = ttk.Combobox(top, textvariable=self.month_var, state="readonly", width=10)
        self.month_box.pack(side="left")
        self.month_box.bind("<<ComboboxSelected>>", lambda e: self._load_month())
        ttk.Button(top, text="Export month...", command=self._on_export).pack(side="left", padx=(14, 0))
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="left", padx=(6, 0))

        self.status_label = ttk.Label(self, text="", padding=(10, 6, 10, 0))
        self.status_label.grid(row=1, column=0, sticky="w")
        self.warn_label = ttk.Label(self, text="", foreground="#b26a00", padding=(10, 0, 10, 0), wraplength=860, justify="left")
        self.warn_label.grid(row=2, column=0, sticky="w")

        site_frame = ttk.Frame(self, padding=(10, 6, 10, 0))
        site_frame.grid(row=3, column=0, sticky="nsew")
        site_frame.rowconfigure(0, weight=1)
        site_frame.columnconfigure(0, weight=1)
        cols = ("site", "name", "tier", "outages", "down", "all", "excl", "note")
        self.site_tree = ttk.Treeview(site_frame, columns=cols, show="headings", selectmode="browse", height=9)
        for col, text, width, anchor in (("site", "Site", 60, "w"), ("name", "Name", 170, "w"), ("tier", "Tier", 70, "w"),
                                         ("outages", "Outages", 65, "e"), ("down", "Down (min)", 80, "e"),
                                         ("all", "Avail (all)", 90, "e"), ("excl", "Avail (excl planned)", 130, "e"),
                                         ("note", "Note", 220, "w")):
            self.site_tree.heading(col, text=text)
            self.site_tree.column(col, width=width, anchor=anchor)
        self.site_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(site_frame, orient="vertical", command=self.site_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.site_tree.configure(yscrollcommand=scroll.set)
        self.site_tree.tag_configure("ongoing", foreground="#c62828")
        self.site_tree.tag_configure("untracked", foreground="#8a8a8a")
        self.site_tree.bind("<<TreeviewSelect>>", self._on_site_selected)

        self.inner = ttk.Notebook(self)
        self.inner.grid(row=4, column=0, sticky="nsew", padx=10, pady=(6, 0))
        detail_page = ttk.Frame(self.inner)
        planned_page = ttk.Frame(self.inner)
        self.inner.add(detail_page, text="Outages of selected site")
        self.inner.add(planned_page, text="Planned windows")
        self._build_detail_page(detail_page)
        self._build_planned_page(planned_page)

        self.tier_label = ttk.Label(self, text="", padding=(10, 6, 10, 10), justify="left")
        self.tier_label.grid(row=5, column=0, sticky="w")

    def _build_detail_page(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        bar = ttk.Frame(page, padding=(0, 4, 0, 4))
        bar.grid(row=0, column=0, sticky="ew")
        self.detail_label = ttk.Label(bar, text="Select a site to see its outages.")
        self.detail_label.pack(side="left")
        self.mark_button = ttk.Button(bar, text="Mark selected outage as planned...", command=self._on_mark, state="disabled")
        self.mark_button.pack(side="right")
        dcols = ("what", "start", "end", "length", "counted", "core", "planned", "how")
        self.detail_tree = ttk.Treeview(page, columns=dcols, show="headings", selectmode="browse", height=5)
        for col, text, width, anchor in (("what", "What", 110, "w"), ("start", "Start", 120, "w"), ("end", "End", 120, "w"),
                                         ("length", "Length (min)", 90, "e"), ("counted", "Counted (min)", 100, "e"),
                                         ("core", "Core unplanned", 100, "e"), ("planned", "Core planned", 90, "e"),
                                         ("how", "How it was built", 220, "w")):
            self.detail_tree.heading(col, text=text)
            self.detail_tree.column(col, width=width, anchor=anchor)
        self.detail_tree.grid(row=1, column=0, sticky="nsew")
        dscroll = ttk.Scrollbar(page, orient="vertical", command=self.detail_tree.yview)
        dscroll.grid(row=1, column=1, sticky="ns")
        self.detail_tree.configure(yscrollcommand=dscroll.set)
        self.detail_tree.tag_configure("ongoing", foreground="#c62828")
        self.detail_tree.bind("<<TreeviewSelect>>", lambda e: self._sync_buttons())

    def _build_planned_page(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        bar = ttk.Frame(page, padding=(0, 4, 0, 4))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="Add window...", command=self._on_add_window).pack(side="left")
        self.edit_button = ttk.Button(bar, text="Edit selected...", command=self._on_edit_window, state="disabled")
        self.edit_button.pack(side="left", padx=(6, 0))
        self.delete_button = ttk.Button(bar, text="Delete selected", command=self._on_delete_window, state="disabled")
        self.delete_button.pack(side="left", padx=(6, 0))
        self.restore_button = ttk.Button(bar, text="Restore selected", command=self._on_restore_window, state="disabled")
        self.restore_button.pack(side="left", padx=(6, 0))
        self.show_deleted_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show deleted", variable=self.show_deleted_var,
                        command=self._fill_planned).pack(side="left", padx=(14, 0))
        self.planned_count_label = ttk.Label(bar, text="")
        self.planned_count_label.pack(side="right")
        pcols = ("id", "sites", "start", "end", "reason", "source", "state")
        self.planned_tree = ttk.Treeview(page, columns=pcols, show="headings", selectmode="browse", height=5)
        for col, text, width, anchor in (("id", "Id", 45, "e"), ("sites", "Sites", 150, "w"), ("start", "Start", 120, "w"),
                                         ("end", "End", 120, "w"), ("reason", "Reason", 240, "w"),
                                         ("source", "Source", 90, "w"), ("state", "State", 70, "w")):
            self.planned_tree.heading(col, text=text)
            self.planned_tree.column(col, width=width, anchor=anchor)
        self.planned_tree.grid(row=1, column=0, sticky="nsew")
        pscroll = ttk.Scrollbar(page, orient="vertical", command=self.planned_tree.yview)
        pscroll.grid(row=1, column=1, sticky="ns")
        self.planned_tree.configure(yscrollcommand=pscroll.set)
        self.planned_tree.tag_configure("deleted", foreground="#8a8a8a")
        self.planned_tree.bind("<<TreeviewSelect>>", lambda e: self._sync_buttons())

    # ------------------------------------------------------------------
    # loading and showing a month
    # ------------------------------------------------------------------

    def refresh(self):
        """Re-read the stored pulls, keep the chosen month when it is still there."""
        keep = self.month_var.get()
        try:
            with db.get_conn() as conn:
                months = rpt.months_covered(conn)
                last = outage_db.last_pull(conn)
        except Exception as e:
            self.status_label.configure(text=f"Could not read the outage data: {type(e).__name__}: {str(e)[:120]}")
            return
        self._month_by_text = {f"{y}-{m:02d}": (y, m) for y, m in months}
        texts = list(self._month_by_text)
        self.month_box.configure(values=texts)
        self._show_pull_status(last)
        self._fill_planned()
        if not texts:
            self.month_var.set("")
            self._report = None
            self._clear_tables()
            self.warn_label.configure(text="Nothing has been pulled yet. Use \"Pull from Orion\".")
            return
        if keep in self._month_by_text:
            self.month_var.set(keep)
        else:
            last_month = pull_month = None
            if last is not None:
                import orion_parser as op
                now = op.parse_time(last["server_now"])
                pull_month = f"{now.year}-{now.month:02d}"
            last_month = pull_month if pull_month in self._month_by_text else texts[-1]
            self.month_var.set(last_month)
        self._load_month()

    def _show_pull_status(self, last):
        if last is None:
            self.status_label.configure(text="No pull stored yet.", foreground="#b26a00")
            return
        pulled = datetime.datetime.fromisoformat(last["pulled_at"]).replace(tzinfo=datetime.timezone.utc)
        age = (datetime.datetime.now(datetime.timezone.utc) - pulled).days
        text = (f"Last pull {age} day(s) ago (Orion time then: {last['server_now']}); "
                f"{last['events_fetched']} events, {last['events_new']} new.")
        stale = age >= pull_orion.STALE_AFTER_DAYS
        if stale:
            text += "  Orion keeps only 30 days, so pull again soon."
        self.status_label.configure(text=text, foreground="#c62828" if stale else "#1b5e20")

    def _clear_tables(self):
        self.site_tree.delete(*self.site_tree.get_children())
        self.detail_tree.delete(*self.detail_tree.get_children())
        self._detail_rows = {}
        self.tier_label.configure(text="")
        self.detail_label.configure(text="Select a site to see its outages.")

    def _load_month(self):
        ym = self._month_by_text.get(self.month_var.get())
        if ym is None:
            return
        try:
            with db.get_conn() as conn:
                self._report = rpt.compute_month(conn, ym[0], ym[1])
        except Exception as e:
            self._report = None
            self._clear_tables()
            self.warn_label.configure(text=f"Could not work out {self.month_var.get()}: {type(e).__name__}: {str(e)[:150]}")
            return
        self._fill_site_table()
        self._fill_planned()
        self.warn_label.configure(text="\n".join(self._report["warnings"]))

    def _fill_site_table(self):
        report = self._report
        keep = self.site_tree.selection()
        self._clear_tables()
        sites, tiers = report["calc"]["sites"], report["tiers"]
        for octet in sorted(sites, key=rpt._site_sort_key):
            e = sites[octet]
            tier = rpt.TIER_LABELS.get(tiers.get(octet), "Untiered")
            if not e["tracked"]:
                self.site_tree.insert("", "end", iid=octet, tags=("untracked",),
                                      values=(octet, e["site_name"] or "", tier, "", "", "", "", f"Not tracked: {e['reason']}"))
                continue
            outs = e["outages"]
            ongoing = any(o["ongoing"] for o in outs)
            self.site_tree.insert("", "end", iid=octet, tags=("ongoing",) if ongoing else (), values=(
                octet, e["site_name"] or "", tier, len(outs), _mins(e["down"]["total"]),
                _pct(e["availability_all"]), _pct(e["availability_excl_planned"]), "ongoing" if ongoing else ""))
        lines = []
        for t in rpt.tier_summary(report):
            lines.append(f"{t['label']}: {t['sites']} site(s), average {_pct(t['avg_all'])} "
                         f"({_pct(t['avg_excl_planned'])} planned excluded), {t['sites_without_outage']} with no outage")
        self.tier_label.configure(text="\n".join(lines))
        if keep and self.site_tree.exists(keep[0]):
            self.site_tree.selection_set(keep[0])

    def _on_site_selected(self, event=None):
        self.detail_tree.delete(*self.detail_tree.get_children())
        self._detail_rows = {}
        self._sync_buttons()
        selected = self.site_tree.selection()
        if not selected or self._report is None:
            self.detail_label.configure(text="Select a site to see its outages.")
            return
        octet = selected[0]
        e = self._report["calc"]["sites"].get(octet)
        if e is None or not e["tracked"]:
            self.detail_label.configure(text=f"Site {octet}: not tracked ({e['reason'] if e else 'unknown'}).")
            return
        self.detail_label.configure(text=f"Site {octet} {e['site_name'] or ''}: {len(e['outages'])} site outage(s), "
                                         f"{len(e['device_outages'])} device outage(s)")
        rows = ([("s%d" % i, "Site down", o) for i, o in enumerate(e["outages"])]
                + [("d%d" % i, o["hostname"] or "Device", o) for i, o in enumerate(e["device_outages"])])
        for iid, what, o in rows:
            self._detail_rows[iid] = o
            self.detail_tree.insert("", "end", iid=iid, tags=("ongoing",) if o["ongoing"] else (), values=(
                what, _when(o["start"]), "ongoing" if o["ongoing"] else _when(o["end"]), _mins(o["minutes"]),
                _mins(o["total"]), _mins(o["core_unplanned"]), _mins(o["core_planned"]),
                outage_calc.describe_parts(o)))

    # ------------------------------------------------------------------
    # planned windows
    # ------------------------------------------------------------------

    def _sync_buttons(self):
        picked = self.detail_tree.selection()
        self.mark_button.configure(state="normal" if picked and picked[0].startswith("s") else "disabled")
        chosen = self.planned_tree.selection()
        w = self._planned_rows.get(chosen[0]) if chosen else None
        self.edit_button.configure(state="normal" if w and not w["deleted"] else "disabled")
        self.delete_button.configure(state="normal" if w and not w["deleted"] else "disabled")
        self.restore_button.configure(state="normal" if w and w["deleted"] else "disabled")

    def _fill_planned(self):
        keep = self.planned_tree.selection()
        self.planned_tree.delete(*self.planned_tree.get_children())
        self._planned_rows = {}
        try:
            with db.get_conn() as conn:
                windows = outage_db.load_planned_windows(conn, include_deleted=self.show_deleted_var.get())
        except Exception as e:
            self.planned_count_label.configure(text=f"Could not read windows: {type(e).__name__}")
            self._sync_buttons()
            return
        month = self._report["window"] if self._report else None
        for w in sorted(windows, key=lambda x: x["start"], reverse=True):
            if month and not (w["start"] < month[1] and w["end"] > month[0]):
                continue
            self._planned_rows[str(w["id"])] = w
            self.planned_tree.insert("", "end", iid=str(w["id"]), tags=("deleted",) if w["deleted"] else (), values=(
                w["id"], _sites_text(w["sites"]), _when(w["start"]), _when(w["end"]), w.get("reason") or "",
                "from outage" if w.get("source") == "from_outage" else "manual", "deleted" if w["deleted"] else ""))
        where = f" touching {self.month_var.get()}" if month else ""
        self.planned_count_label.configure(text=f"{len(self._planned_rows)} window(s){where}")
        if keep and self.planned_tree.exists(keep[0]):
            self.planned_tree.selection_set(keep[0])
        self._sync_buttons()

    def _snapshot(self, conn):
        """{site: (availability planned excluded, core planned minutes)} for the loaded month, or None."""
        if self._report is None:
            return None
        report = rpt.compute_month(conn, self._report["year"], self._report["month"])
        return {octet: (e["availability_excl_planned"], round(e["down"]["core_planned"], 2))
                for octet, e in report["calc"]["sites"].items() if e["tracked"]}

    def _impact_text(self, before, after):
        if before is None or after is None:
            return "No month is loaded, so the effect on availability is not shown."
        changed = [o for o in sorted(after, key=rpt._site_sort_key) if o in before and before[o] != after[o]]
        if not changed:
            return f"This does not change any site's numbers for {self.month_var.get()}."
        lines = [f"Effect on {self.month_var.get()} (availability with planned downtime excluded):"]
        for o in changed[:8]:
            lines.append(f"  Site {o}: {_pct(before[o][0])} -> {_pct(after[o][0])}")
        if len(changed) > 8:
            lines.append(f"  ... and {len(changed) - 8} more site(s)")
        return "\n".join(lines)

    def _commit(self, action, payload, trial, title):
        """Try the change in a transaction that is rolled back, show its effect, ask, then
        queue it. Returns None when saved, or a plain-text problem."""
        try:
            with db.get_conn() as conn:
                before = self._snapshot(conn)
                trial(conn)
                after = self._snapshot(conn)
                conn.rollback()
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"{type(e).__name__}: {str(e)[:160]}"
        if not messagebox.askyesno(title, self._impact_text(before, after) + "\n\nSave this change?"):
            return "Not saved. Change the values, or Cancel."
        try:
            write_queue.queue_and_wait(action, timeout=60, **payload)
        except (RuntimeError, TimeoutError, ValueError) as e:
            return f"Saving failed: {str(e)[:200]}"
        self._load_month()
        self._fill_planned()
        return None

    def _add_values(self, values, source):
        try:
            sites, start, end, reason = outage_db.validate_window(
                values["sites"], _parse_when(values["start"], "Start"), _parse_when(values["end"], "End"), values["reason"])
        except ValueError as e:
            return str(e)
        payload = {"sites": sites, "start": outage_db.fmt_time(start), "end": outage_db.fmt_time(end),
                   "reason": reason, "source": source}
        return self._commit("add_planned_window", payload,
                            lambda c: outage_db.add_planned_window(c, sites, start, end, reason, source), "Add planned window")

    def _on_add_window(self):
        WindowDialog(self, "Add planned window", lambda v: self._add_values(v, "manual"),
                     note="Planned downtime for one or more sites (or all). It can be entered before the work happens.")

    def _on_mark(self):
        picked = self.detail_tree.selection()
        chosen = self.site_tree.selection()
        if not picked or not chosen or picked[0] not in self._detail_rows:
            return
        o, octet = self._detail_rows[picked[0]], chosen[0]
        note = f"Site {octet} outage that began {_when(o['start'])}."
        if o["ongoing"]:
            note += " It is still going, so enter when the planned work finishes."
        note += " Trim the start or end if only part of it was planned."
        WindowDialog(self, "Mark outage as planned", lambda v: self._add_values(v, "from_outage"), sites=octet,
                     start=_when(o["start"]), end="" if o["ongoing"] else _when(o["end"]), sites_locked=True, note=note)

    def _selected_window(self):
        chosen = self.planned_tree.selection()
        return self._planned_rows.get(chosen[0]) if chosen else None

    def _on_edit_window(self):
        w = self._selected_window()
        if w is None or w["deleted"]:
            return

        def on_save(v):
            try:
                start, end = _parse_when(v["start"], "Start"), _parse_when(v["end"], "End")
                outage_db.validate_window(["x"], start, end, v["reason"])
            except ValueError as e:
                return str(e)
            payload = {"window_id": w["id"], "start": outage_db.fmt_time(start), "end": outage_db.fmt_time(end),
                       "reason": v["reason"]}
            return self._commit("update_planned_window", payload,
                                lambda c: outage_db.update_planned_window(c, w["id"], start, end, v["reason"]),
                                "Change planned window")
        WindowDialog(self, f"Edit planned window {w['id']}", on_save, sites=_sites_text(w["sites"]), start=_when(w["start"]),
                     end=_when(w["end"]), reason=w.get("reason") or "", sites_locked=True,
                     note="Change the times or the reason. To change which sites it covers, delete it and add a new one.")

    def _on_delete_window(self):
        w = self._selected_window()
        if w is None or w["deleted"]:
            return
        problem = self._commit("delete_planned_window", {"window_id": w["id"]},
                               lambda c: outage_db.delete_planned_window(c, w["id"]), f"Delete planned window {w['id']}")
        if problem:
            messagebox.showerror("Delete planned window", problem)

    def _on_restore_window(self):
        w = self._selected_window()
        if w is None or not w["deleted"]:
            return
        problem = self._commit("restore_planned_window", {"window_id": w["id"]},
                               lambda c: outage_db.restore_planned_window(c, w["id"]), f"Restore planned window {w['id']}")
        if problem:
            messagebox.showerror("Restore planned window", problem)

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------

    def _on_export(self):
        if self._report is None:
            messagebox.showinfo("Export month", "There is no month loaded to export.")
            return
        r = self._report
        out_dir = os.path.join(HERE, "outage_reports")
        os.makedirs(out_dir, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="Export month", initialdir=out_dir, initialfile=f"outage_report_{r['year']}-{r['month']:02d}.xlsx",
            defaultextension=".xlsx", filetypes=[("Excel workbook", "*.xlsx"), ("CSV files (one per sheet)", "*.csv")])
        if not path:
            return
        try:
            sheets = rpt.build_sheets(r)
            if path.lower().endswith(".csv"):
                base = os.path.splitext(os.path.basename(path))[0]
                files = xlsx_writer.write_csv_files(os.path.dirname(path), base, sheets)
                messagebox.showinfo("Export month", f"Wrote {len(files)} CSV files to {os.path.dirname(path)}")
            else:
                xlsx_writer.write_xlsx(path, sheets)
                messagebox.showinfo("Export month", f"Wrote {path}")
        except Exception as e:
            messagebox.showerror("Export month", f"Could not write the file: {type(e).__name__}: {str(e)[:200]}")

    # ------------------------------------------------------------------
    # pull from Orion (background thread)
    # ------------------------------------------------------------------

    def _orion_cfg(self):
        try:
            return settings_loader.get_section("orion")
        except settings_loader.SettingsError as e:
            messagebox.showerror("Orion", f"{e}")
            return None

    def _get_credentials(self):
        entry = next((v for k, v in self._creds.items() if k.lower() == "tacacs"), None)
        if not entry:
            raise ValueError('The credential store has no "tacacs" entry. Add one on the Credentials tab.')
        fields = {k.lower(): v for k, v in entry.items()}
        values = []
        for key in ("username", "password"):
            field = fields.get(key)
            value = field.get("value") if isinstance(field, dict) else field
            if not value:
                raise ValueError(f'The "tacacs" login has no "{key}" field (or it is empty).')
            values.append(str(value))
        return values[0], values[1]

    def _on_pull(self):
        if self._busy:
            return
        cfg = self._orion_cfg()
        if cfg is None:
            return
        host = str(cfg.get("host") or "").strip()
        if not host:
            messagebox.showerror("Orion", 'No Orion host. Add "orion": {"host": "NAME"} to settings.json.')
            return
        try:
            user, password = self._get_credentials()
        except ValueError as e:
            messagebox.showerror("Orion", str(e))
            return
        self._busy = True
        self.pull_button.configure(state="disabled")
        self.status_label.configure(text=f"Pulling from {host} ...", foreground="#555555")
        threading.Thread(target=self._pull_job, args=(host, user, password, cfg), daemon=True).start()

    def _pull_job(self, host, user, password, cfg):
        try:
            client = oc.OrionClient(host, user, password, port=int(cfg.get("port") or oc.DEFAULT_PORT),
                                    verify_tls=bool(cfg.get("verify_tls", False)),
                                    timeout=float(cfg.get("timeout_seconds") or 120), page_size=2000)
            with db.get_conn() as conn:
                fetched = pull_orion.fetch_pull(client, conn, 30)
            if fetched["problems"]:
                self._queue.put(("failed", "Orion's answer failed a safety check, so nothing was stored:\n- "
                                 + "\n- ".join(fetched["problems"])))
                return
            summary = write_queue.queue_and_wait("store_orion_pull", timeout=120, **fetched["payload"])
            text = (f"Stored {summary['events_new']} new event(s) of {summary['events_seen']} fetched.")
            if fetched["warnings"]:
                text += "\n\nNotes:\n- " + "\n- ".join(fetched["warnings"])
            self._queue.put(("done", text))
        except oc.OrionError as e:
            self._queue.put(("failed", f"Orion: {e}"))
        except Exception as e:
            self._queue.put(("failed", f"{type(e).__name__}: {str(e)[:200]}"))

    def _poll(self):
        try:
            while True:
                kind, text = self._queue.get_nowait()
                self._busy = False
                self.pull_button.configure(state="normal")
                self.refresh()
                if kind == "done":
                    messagebox.showinfo("Pull from Orion", text)
                else:
                    messagebox.showerror("Pull from Orion", text)
        except queue.Empty:
            pass
        self.after(200, self._poll)