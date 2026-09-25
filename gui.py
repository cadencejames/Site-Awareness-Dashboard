"""
gui.py - Tkinter desktop app for Site Awareness Dashboard management.

Five tabs: Site Manager (name/code a site, set the CDP seed, set/clear
the ARP-seed override, set/clear a device's ARP override IP, auto-flag
seeds for single-device sites), Discovery (run CDP/ARP collection
against one site or all of them, with live worker-status columns and
console output), Export (write the current inventory out to CSV),
Command Runner (preview/commit a plugin's changes against one site or
all of them), and Credentials (unlock status and store maintenance).

Discovery deliberately reuses orchestrator.py's run_for_sites()/
discover_site()/collect_arp() directly rather than reimplementing any
of that logic - this GUI is purely a scope/action picker, a background
thread so the window doesn't freeze during a multi-minute run, and a
console panel plus worker-status display that reflect those functions'
existing print() output and progress_cb callbacks. There is exactly
one implementation of the actual discovery logic (orchestrator.py),
not two.

Run from the project root:
    python3 gui.py

Requires credentials.enc to already exist (via
utilities/credential_manager.py) before running anything on the
Discovery tab; the Site Manager tab itself needs no credentials.
"""

import sys
import os
import re
import getpass
import ipaddress
import threading
import queue
import subprocess
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, scrolledtext

from cryptography.exceptions import InvalidTag

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utilities"))

import db  # noqa: E402
import credential_store  # noqa: E402
import credential_loader  # noqa: E402
import orchestrator  # noqa: E402
import dashboard_generate  # noqa: E402
import csv_export  # noqa: E402
import write_queue  # noqa: E402


CLEAR = object()  # sentinel: "the user chose to clear/reset", distinct from None (cancelled)
_DONE = object()  # sentinel pushed onto the output queue when a discovery run finishes

DRAIN_INTERVAL_MS = 10_000  # how often App's background timer tries to drain the shared write queue

# DiscoveryTab's per-worker status columns: one fixed (background,
# foreground, abbreviated label) triple per phase, keyed by the same
# phase strings orchestrator.py's progress_cb reports (see
# _report_progress()'s docstring there). "idle"/"queued" are GUI-only
# phases (a worker slot that hasn't picked up its first site yet) -
# orchestrator.py never reports those itself.
PHASE_STYLES = {
    "idle":        ("#f0f0f0", "#888888", "Idle"),
    "queued":      ("#f0f0f0", "#888888", "Queued"),
    "connecting":  ("#bbdefb", "#0d47a1", "Connecting"),
    "walking":     ("#ffe0b2", "#e65100", "Walking"),
    "correlating": ("#e1bee7", "#4a148c", "Correlating"),
    "arp":         ("#b2dfdb", "#00695c", "ARP"),
    "done":        ("#c8e6c9", "#1b5e20", "Done"),
    "error":       ("#ffcdd2", "#b71c1c", "Error"),
    "skipped":     ("#eeeeee", "#616161", "Skipped"),
}
STALE_WORKER_SECONDS = 90  # how long a worker can sit on one status before its column is flagged as possibly stuck
STALE_CHECK_INTERVAL_MS = 5_000
# Phases a stuck-timer should never flag - each one means "this worker
# genuinely has nothing further to report," not "it's gone quiet while
# still working," so highlighting them as stuck would just be noise.
_TERMINAL_PHASES = {"idle", "queued", "done", "error", "skipped"}


class DevicePickerDialog(tk.Toplevel):
    """Modal dialog: pick one device from a site's device list. If
    allow_clear is True, also offers a "Clear override" button.

    Sets self.result to:
      - the chosen device row, or
      - the CLEAR sentinel if "Clear override" was clicked, or
      - None if cancelled/closed without a choice.
    """

    def __init__(self, parent, title: str, prompt: str, devices: list, allow_clear: bool = False):
        super().__init__(parent)
        self.title(title)
        self.result = None
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        ttk.Label(self, text=prompt, padding=(12, 12, 12, 4)).pack(anchor="w")

        list_frame = ttk.Frame(self, padding=(12, 0, 12, 8))
        list_frame.pack(fill="both", expand=True)

        self.listbox = tk.Listbox(list_frame, width=60, height=min(10, max(4, len(devices))), exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        self._devices = devices
        for d in devices:
            markers = []
            if d["is_seed"]:
                markers.append("CDP seed")
            if d["is_arp_seed"]:
                markers.append("ARP seed")
            if d["arp_override_ip"]:
                markers.append(f"override={d['arp_override_ip']}")
            suffix = f"  [{', '.join(markers)}]" if markers else ""
            self.listbox.insert("end", f"{d['hostname']}  ({d['mgmt_ip'] or 'no IP'}){suffix}")

        if devices:
            self.listbox.selection_set(0)
        self.listbox.bind("<Double-Button-1>", lambda e: self._on_ok())

        button_row = ttk.Frame(self, padding=(12, 0, 12, 12))
        button_row.pack(fill="x")

        if allow_clear:
            ttk.Button(button_row, text="Clear override", command=self._on_clear).pack(side="left")

        ttk.Button(button_row, text="Cancel", command=self._on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(button_row, text="OK", command=self._on_ok).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.wait_window(self)

    def _on_ok(self):
        selection = self.listbox.curselection()
        if selection:
            self.result = self._devices[selection[0]]
        self.destroy()

    def _on_clear(self):
        self.result = CLEAR
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()


class SiteManagerTab(ttk.Frame):
    """Manual, per-site bookkeeping: name/code a site, flag its CDP
    seed device (and, if different, its ARP seed or a device's ARP
    override IP), and review/reassign whatever's landed under the
    reserved "Unassigned" site. Reads and writes go straight through
    db.py/write_queue.py - this tab has no logic of its own beyond
    presenting and editing that data.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.selected_site = None
        self.selected_device = None
        self._sites_by_iid = {}
        self._devices_by_iid = {}
        self._build_layout()
        self.refresh_sites()

    def _build_layout(self):
        self.columnconfigure(0, weight=1, minsize=280)
        self.columnconfigure(1, weight=2)
        self.rowconfigure(0, weight=1)

        # --- Left pane: site list ---
        left = ttk.Frame(self, padding=10)
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        ttk.Label(left, text="Sites").grid(row=0, column=0, sticky="w")

        self.site_tree = ttk.Treeview(left, columns=("octet", "name", "code"), show="headings", selectmode="browse")
        self.site_tree.heading("octet", text="Octet")
        self.site_tree.heading("name", text="Name")
        self.site_tree.heading("code", text="Code")
        self.site_tree.column("octet", width=60, anchor="w")
        self.site_tree.column("name", width=140, anchor="w")
        self.site_tree.column("code", width=70, anchor="w")
        self.site_tree.grid(row=1, column=0, sticky="nsew", pady=(4, 4))
        self.site_tree.bind("<<TreeviewSelect>>", self._on_site_selected)
        self._enable_sorting(self.site_tree, {
            "octet": self._sort_key_int_prefix,
            "name": self._sort_key_str,
            "code": self._sort_key_str,
        })

        site_scroll = ttk.Scrollbar(left, orient="vertical", command=self.site_tree.yview)
        self.site_tree.configure(yscrollcommand=site_scroll.set)
        site_scroll.grid(row=1, column=1, sticky="ns")

        auto_flag_link = ttk.Label(left, text="Auto-flag single-device sites", foreground="#3366cc", cursor="hand2")
        auto_flag_link.grid(row=2, column=0, sticky="w", pady=(4, 0))
        auto_flag_link.bind("<Button-1>", lambda e: self._auto_flag_singletons())

        site_button_row = ttk.Frame(left)
        site_button_row.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        site_button_row.columnconfigure(0, weight=1)
        site_button_row.columnconfigure(1, weight=1)
        ttk.Button(site_button_row, text="Add Site", command=self._add_site).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        self.btn_delete_site = ttk.Button(
            site_button_row, text="Delete Site", command=self._delete_site, state="disabled"
        )
        self.btn_delete_site.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        # --- Right pane: actions (fixed) + devices (scrolls) ---
        right = ttk.Frame(self, padding=10)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)

        self.actions_label = ttk.Label(right, text="Actions")
        self.actions_label.grid(row=0, column=0, sticky="w")

        actions_frame = ttk.Frame(right)
        actions_frame.grid(row=1, column=0, sticky="ew", pady=(4, 14))
        actions_frame.columnconfigure(0, weight=1)
        actions_frame.columnconfigure(1, weight=1)

        self.btn_identity = ttk.Button(actions_frame, text="Name / code site", command=self._set_identity, state="disabled")
        self.btn_identity.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))

        self.btn_seed = ttk.Button(actions_frame, text="Set CDP seed", command=self._set_seed, state="disabled")
        self.btn_seed.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 4))

        self.btn_arp_seed = ttk.Button(actions_frame, text="Set ARP seed", command=self._set_arp_seed, state="disabled")
        self.btn_arp_seed.grid(row=1, column=0, sticky="ew", padx=(0, 4))

        self.btn_arp_override = ttk.Button(actions_frame, text="Set ARP override IP", command=self._set_arp_override_ip, state="disabled")
        self.btn_arp_override.grid(row=1, column=1, sticky="ew", padx=(4, 0))

        devices_header = ttk.Frame(right)
        devices_header.grid(row=2, column=0, sticky="ew")
        devices_header.columnconfigure(0, weight=1)
        ttk.Label(devices_header, text="Devices").grid(row=0, column=0, sticky="w")
        self.btn_add_device = ttk.Button(devices_header, text="Add Device", command=self._add_device, state="disabled")
        self.btn_add_device.grid(row=0, column=1, sticky="e", padx=(0, 6))
        self.btn_delete_device = ttk.Button(devices_header, text="Delete Device", command=self._delete_device, state="disabled")
        self.btn_delete_device.grid(row=0, column=2, sticky="e", padx=(0, 6))
        self.btn_mark_stale = ttk.Button(devices_header, text="Mark Stale", command=self._toggle_mark_stale, state="disabled")
        self.btn_mark_stale.grid(row=0, column=3, sticky="e", padx=(0, 6))
        self.btn_open_ssh = ttk.Button(devices_header, text="Open SSH", command=self._open_ssh, state="disabled")
        self.btn_open_ssh.grid(row=0, column=4, sticky="e")

        device_frame = ttk.Frame(right)
        device_frame.grid(row=3, column=0, sticky="nsew", pady=(4, 0))
        device_frame.rowconfigure(0, weight=1)
        device_frame.columnconfigure(0, weight=1)

        self.device_tree = ttk.Treeview(device_frame, columns=("hostname", "ip", "role", "override"), show="headings")
        self.device_tree.heading("hostname", text="Hostname")
        self.device_tree.heading("ip", text="IP")
        self.device_tree.heading("role", text="Role")
        self.device_tree.heading("override", text="ARP Override")
        self.device_tree.column("hostname", width=200, anchor="w")
        self.device_tree.column("ip", width=110, anchor="w")
        self.device_tree.column("role", width=130, anchor="w")
        self.device_tree.column("override", width=110, anchor="w")
        self.device_tree.grid(row=0, column=0, sticky="nsew")
        self.device_tree.bind("<<TreeviewSelect>>", self._on_device_selected)
        self._enable_sorting(self.device_tree, {
            "hostname": self._sort_key_str,
            "ip": self._sort_key_ip,
            "role": self._sort_key_str,
            "override": self._sort_key_ip,
        })

        dev_scroll = ttk.Scrollbar(device_frame, orient="vertical", command=self.device_tree.yview)
        self.device_tree.configure(yscrollcommand=dev_scroll.set)
        dev_scroll.grid(row=0, column=1, sticky="ns")

    # --- data population ---

    def refresh_sites(self):
        previously_selected_id = self.selected_site["id"] if self.selected_site else None
        for row in self.site_tree.get_children():
            self.site_tree.delete(row)
        self._sites_by_iid = {}
        with db.get_conn() as conn:
            sites = db.get_all_sites(conn)
        for site in sites:
            iid = str(site["id"])
            self.site_tree.insert("", "end", iid=iid, values=(
                site["site_octet"],
                site["site_name"] or "(unset)",
                site["site_code"] or "-",
            ))
            self._sites_by_iid[iid] = site
        if previously_selected_id is not None and str(previously_selected_id) in self._sites_by_iid:
            self.site_tree.selection_set(str(previously_selected_id))
        else:
            self.selected_site = None
            self._set_actions_enabled(False)
            self.actions_label.config(text="Actions")
            self._clear_devices()

    def _on_site_selected(self, event=None):
        selection = self.site_tree.selection()
        if not selection:
            self.selected_site = None
            self._set_actions_enabled(False)
            self.actions_label.config(text="Actions")
            self._clear_devices()
            return
        site = self._sites_by_iid[selection[0]]
        self.selected_site = site
        label = site["site_name"] or site["site_octet"]
        self.actions_label.config(text=f"Actions - {label} ({site['site_octet']})")
        self._set_actions_enabled(True)
        # "Unassigned" is a reserved site used for cross-site link
        # attribution - never offer it as a deletable target, or a
        # future CDP walk that lands an unattributed neighbor there
        # would have nowhere to go.
        is_unassigned = site["site_octet"] == db.UNASSIGNED_SITE_OCTET
        self.btn_delete_site.configure(state="disabled" if is_unassigned else "normal")
        self.refresh_devices()

    def _set_actions_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for btn in (self.btn_identity, self.btn_seed, self.btn_arp_seed, self.btn_arp_override, self.btn_add_device):
            btn.config(state=state)
        if not enabled:
            self.btn_delete_site.configure(state="disabled")

    def _clear_devices(self):
        for row in self.device_tree.get_children():
            self.device_tree.delete(row)
        self._devices_by_iid = {}
        self.selected_device = None
        self.btn_open_ssh.configure(state="disabled")
        self.btn_delete_device.configure(state="disabled")
        self.btn_mark_stale.configure(state="disabled", text="Mark Stale")

    def refresh_devices(self):
        self._clear_devices()
        if self.selected_site is None:
            return
        with db.get_conn() as conn:
            devices = db.get_devices_for_site(conn, self.selected_site["id"])
        for d in devices:
            roles = []
            if d["is_seed"]:
                roles.append("CDP seed")
            if d["is_arp_seed"]:
                roles.append("ARP seed")
            if d["marked_stale_at"]:
                roles.append("STALE")
            iid = str(d["id"])
            self.device_tree.insert("", "end", iid=iid, values=(
                d["hostname"], d["mgmt_ip"] or "-", ", ".join(roles), d["arp_override_ip"] or "-",
            ))
            self._devices_by_iid[iid] = d

    def _on_device_selected(self, event=None):
        selection = self.device_tree.selection()
        if not selection:
            self.selected_device = None
            self.btn_open_ssh.configure(state="disabled")
            self.btn_delete_device.configure(state="disabled")
            self.btn_mark_stale.configure(state="disabled", text="Mark Stale")
            return
        self.selected_device = self._devices_by_iid[selection[0]]
        self.btn_open_ssh.configure(state="normal")
        self.btn_delete_device.configure(state="normal")
        self.btn_mark_stale.configure(
            state="normal",
            text="Clear Stale" if self.selected_device["marked_stale_at"] else "Mark Stale",
        )

    # --- actions ---

    def _pick_device(self, title: str, prompt: str, allow_clear: bool = False):
        with db.get_conn() as conn:
            devices = db.get_devices_for_site(conn, self.selected_site["id"])
        if not devices:
            messagebox.showinfo(title, "No devices found for this site.")
            return None
        dialog = DevicePickerDialog(self, title, prompt, devices, allow_clear=allow_clear)
        return dialog.result

    def _set_identity(self):
        if self.selected_site is None:
            return
        site = self.selected_site
        name = simpledialog.askstring(
            "Name / code site",
            f"New site_name for octet {site['site_octet']} (blank to leave unchanged):",
            parent=self,
        )
        if name is None:
            return  # cancelled
        code = simpledialog.askstring(
            "Name / code site",
            f"New site_code for octet {site['site_octet']} (blank to leave unchanged):",
            parent=self,
        )
        if code is None:
            return  # cancelled
        try:
            applied, _ = write_queue.queue_and_apply(
                "set_site_identity", site_id=site["id"],
                site_name=name.strip() or None, site_code=code.strip() or None,
            )
        except RuntimeError as e:
            messagebox.showerror("Could not update", str(e))
            return
        if not applied:
            messagebox.showinfo("Name / code site", "Queued - the write queue is busy, this will apply shortly.")
        self.refresh_sites()

    def _set_seed(self):
        if self.selected_site is None:
            return
        result = self._pick_device("Set CDP seed", "Choose the CDP seed device:")
        if result is None or result is CLEAR:
            return
        try:
            applied, _ = write_queue.queue_and_apply(
                "set_device_as_seed", site_id=self.selected_site["id"], device_id=result["id"],
            )
        except RuntimeError as e:
            messagebox.showerror("Set CDP seed", str(e))
            return
        if not applied:
            messagebox.showinfo("Set CDP seed", "Queued - the write queue is busy, this will apply shortly.")
        self.refresh_devices()

    def _set_arp_seed(self):
        if self.selected_site is None:
            return
        result = self._pick_device(
            "Set ARP seed",
            "Choose the ARP-seed override device, or clear to use the CDP seed:",
            allow_clear=True,
        )
        if result is None:
            return
        try:
            if result is CLEAR:
                applied, _ = write_queue.queue_and_apply(
                    "clear_device_as_arp_seed", site_id=self.selected_site["id"],
                )
            else:
                applied, _ = write_queue.queue_and_apply(
                    "set_device_as_arp_seed", site_id=self.selected_site["id"], device_id=result["id"],
                )
        except RuntimeError as e:
            messagebox.showerror("Set ARP seed", str(e))
            return
        if not applied:
            messagebox.showinfo("Set ARP seed", "Queued - the write queue is busy, this will apply shortly.")
        self.refresh_devices()

    def _set_arp_override_ip(self):
        if self.selected_site is None:
            return
        result = self._pick_device("Set ARP override IP", "Choose the device to set an override IP on:")
        if result is None or result is CLEAR:
            return
        device = result
        current = device["arp_override_ip"]
        prompt = f"New override IP for '{device['hostname']}'"
        prompt += f" (currently {current}, blank to clear):" if current else " (blank to leave unset):"
        new_ip = simpledialog.askstring("Set ARP override IP", prompt, parent=self)
        if new_ip is None:
            return  # cancelled
        try:
            applied, _ = write_queue.queue_and_apply(
                "set_device_arp_override_ip", device_id=device["id"], ip=new_ip.strip() or None,
            )
        except RuntimeError as e:
            messagebox.showerror("Set ARP override IP", str(e))
            return
        if not applied:
            messagebox.showinfo("Set ARP override IP", "Queued - the write queue is busy, this will apply shortly.")
        self.refresh_devices()

    def _add_site(self):
        octet = simpledialog.askstring(
            "Add Site",
            "Site octet (required - the numeric key used to attribute discovered devices to this site):",
            parent=self,
        )
        if octet is None:
            return  # cancelled
        octet = octet.strip()
        if not octet:
            messagebox.showwarning("Add Site", "Site octet is required.")
            return
        if octet.lower() == db.UNASSIGNED_SITE_OCTET:
            messagebox.showwarning("Add Site", "That octet is reserved for the built-in 'Unassigned' site.")
            return
        name = simpledialog.askstring("Add Site", "Site name (optional):", parent=self)
        if name is None:
            return  # cancelled
        code = simpledialog.askstring("Add Site", "Site code (optional):", parent=self)
        if code is None:
            return  # cancelled
        # Read-only, so this goes straight against the live db rather
        # than through the write queue - only writes need to queue.
        with db.get_conn() as conn:
            existing = db.get_site_by_octet(conn, octet)
        try:
            applied, _ = write_queue.queue_and_apply(
                "add_site_manual", site_octet=octet,
                site_name=name.strip() or None, site_code=code.strip() or None,
            )
        except RuntimeError as e:
            messagebox.showerror("Add Site", str(e))
            return
        if applied and existing is not None and existing["deleted_at"]:
            messagebox.showinfo("Add Site", f"Site {octet} had been deleted - it's been restored.")
        elif not applied:
            messagebox.showinfo("Add Site", f"Queued - site {octet} will be added shortly.")
        self.refresh_sites()

    def _delete_site(self):
        if self.selected_site is None:
            return
        site = self.selected_site
        if site["site_octet"] == db.UNASSIGNED_SITE_OCTET:
            return  # belt-and-suspenders - button is disabled for this case already
        with db.get_conn() as conn:
            impact = db.get_site_delete_impact(conn, site["id"])
        label = site["site_name"] or site["site_octet"]
        if impact["devices"]:
            device_lines = "\n".join(
                f"  - {d['hostname']} ({d['mgmt_ip'] or 'no IP'})" for d in impact["devices"]
            )
            devices_summary = f"{len(impact['devices'])} device(s) will also be deleted:\n{device_lines}\n\n"
        else:
            devices_summary = "No devices are currently attached to this site.\n\n"
        message = (
            f"Delete site {label} ({site['site_octet']})?\n\n"
            f"{devices_summary}"
            f"{impact['links']} link(s) and {impact['clients']} client record(s) will be hidden along with it.\n\n"
            "This is a soft delete - nothing is permanently destroyed, and it can be restored later "
            "by an administrator if needed."
        )
        if not messagebox.askyesno("Delete Site", message, icon="warning"):
            return
        try:
            applied, _ = write_queue.queue_and_apply("soft_delete_site", site_id=site["id"])
        except RuntimeError as e:
            messagebox.showerror("Delete Site", str(e))
            return
        if not applied:
            messagebox.showinfo("Delete Site", "Queued - the write queue is busy, this will apply shortly.")
        self.selected_site = None
        self.refresh_sites()

    def _add_device(self):
        if self.selected_site is None:
            return
        site = self.selected_site
        hostname = simpledialog.askstring(
            "Add Device", f"Hostname (required, site {site['site_octet']}):", parent=self
        )
        if hostname is None:
            return  # cancelled
        hostname = hostname.strip()
        if not hostname:
            messagebox.showwarning("Add Device", "Hostname is required.")
            return
        mgmt_ip = simpledialog.askstring("Add Device", "Management IP (optional):", parent=self)
        if mgmt_ip is None:
            return  # cancelled
        platform = simpledialog.askstring("Add Device", "Platform (optional, e.g. IOS-XE, NX-OS):", parent=self)
        if platform is None:
            return  # cancelled
        # Read-only, so this goes straight against the live db rather
        # than through the write queue - only writes need to queue.
        with db.get_conn() as conn:
            existing = db.get_device_by_hostname(conn, site["id"], hostname)
        try:
            applied, _ = write_queue.queue_and_apply(
                "add_device_manual", site_id=site["id"], hostname=hostname,
                mgmt_ip=mgmt_ip.strip() or None, platform=platform.strip() or None,
            )
        except RuntimeError as e:
            messagebox.showerror("Add Device", str(e))
            return
        if applied and existing is not None and existing["deleted_at"]:
            messagebox.showinfo("Add Device", f"Device {hostname} had been deleted - it's been restored.")
        elif not applied:
            messagebox.showinfo("Add Device", f"Queued - device {hostname} will be added shortly.")
        self.refresh_devices()

    def _delete_device(self):
        if self.selected_device is None:
            return
        device = self.selected_device
        with db.get_conn() as conn:
            impact = db.get_device_delete_impact(conn, device["id"])
        message = (
            f"Delete device {device['hostname']} ({device['mgmt_ip'] or 'no IP'})?\n\n"
            f"{impact['links']} link(s) and {impact['clients']} client record(s) will be hidden along with it.\n\n"
            "This is a soft delete - nothing is permanently destroyed, and it can be restored later "
            "by an administrator if needed."
        )
        if not messagebox.askyesno("Delete Device", message, icon="warning"):
            return
        try:
            applied, _ = write_queue.queue_and_apply("soft_delete_device", device_id=device["id"])
        except RuntimeError as e:
            messagebox.showerror("Delete Device", str(e))
            return
        if not applied:
            messagebox.showinfo("Delete Device", "Queued - the write queue is busy, this will apply shortly.")
        self.refresh_devices()

    def _auto_flag_singletons(self):
        try:
            applied, flagged = write_queue.queue_and_apply("auto_seed_singleton_sites")
        except RuntimeError as e:
            messagebox.showerror("Auto-flag single-device sites", str(e))
            return
        if not applied:
            messagebox.showinfo(
                "Auto-flag single-device sites", "Queued - the write queue is busy, this will apply shortly."
            )
            return
        if not flagged:
            messagebox.showinfo(
                "Auto-flag single-device sites",
                "Nothing to do - no single-device sites without a seed already set.",
            )
            return
        lines = "\n".join(f"- {site['site_octet']}: {device['hostname']}" for site, device in flagged)
        messagebox.showinfo(
            "Auto-flag single-device sites",
            f"Flagged seed for {len(flagged)} site(s):\n\n{lines}",
        )
        self.refresh_sites()
        self.refresh_devices()

    def _open_ssh(self):
        """Launch a real, external SSH terminal to the selected device -
        deliberately NOT a terminal built into this app (real device
        CLIs need proper terminal emulation - cursor control, --More--
        pagination, tab-completion redraws - that a Tkinter Text widget
        can't give them without essentially reimplementing a terminal
        emulator from scratch). No credentials are passed at all, not
        even a username - this tab has no credential store unlocked,
        and the device will prompt for both normally. Each click opens
        its own independent window; nothing here tracks "the" session,
        so opening several at once to different devices just works.
        """
        if self.selected_device is None:
            return
        with db.get_conn() as conn:
            ip_candidates = db.get_ips_for_device(conn, self.selected_device["id"])
        if not ip_candidates:
            messagebox.showwarning("Open SSH", f"No known IP for '{self.selected_device['hostname']}'.")
            return
        ip = ip_candidates[0]  # top-ranked, same trust logic as everywhere else in this project
        try:
            subprocess.Popen(
                ["powershell", "-NoExit", "-Command", f"ssh {ip}"],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        except FileNotFoundError:
            messagebox.showerror(
                "Open SSH",
                "Could not launch PowerShell/ssh - make sure ssh.exe is installed and on PATH.",
            )

    def _toggle_mark_stale(self):
        """Manually flag (or clear) the selected device as stale -
        independent of the dashboard's automatic age-based check. Any
        link touching this device picks up the same flag automatically
        the next time the dashboard is generated (derived, not stored
        on the link itself), so there's nothing else to update here.
        """
        if self.selected_device is None:
            return
        device_id = self.selected_device["id"]  # captured before refresh_devices() clears the selection
        currently_marked = bool(self.selected_device["marked_stale_at"])
        try:
            applied, _ = write_queue.queue_and_apply(
                "set_device_marked_stale", device_id=device_id, stale=not currently_marked,
            )
        except RuntimeError as e:
            messagebox.showerror("Mark Stale", str(e))
            return
        if not applied:
            messagebox.showinfo("Mark Stale", "Queued - the write queue is busy, this will apply shortly.")
        self.refresh_devices()
        # Re-select the same device so the button/tree reflect the new
        # state immediately instead of reverting to "nothing selected".
        iid = str(device_id)
        if iid in self._devices_by_iid:
            self.device_tree.selection_set(iid)
            self._on_device_selected()

    # --- column sorting ---

    @staticmethod
    def _sort_key_str(value):
        return str(value).lower()

    _LEADING_INT_RE = re.compile(r"^\s*([+-]?\d+)")

    @classmethod
    def _sort_key_int_prefix(cls, value):
        """For values like a site_octet that are usually numeric but can
        be an override key (e.g. "170a", "unassigned") - matches
        db.py's ORDER BY CAST(site_octet AS INTEGER) exactly: reads
        the LEADING numeric portion of the string (SQLite's CAST
        behavior), not "the whole string must be a number" (Python's
        plain int() would reject "170a" outright and misfile it with
        fully non-numeric values like "unassigned" instead of sorting
        it next to "170"). Anything with no leading digits (like
        "unassigned") reads as 0, same as SQLite.
        """
        match = cls._LEADING_INT_RE.match(str(value))
        leading_int = int(match.group(1)) if match else 0
        return (leading_int, str(value).lower())

    @staticmethod
    def _sort_key_ip(value):
        """IP-aware sort so 10.20.0.9 correctly sorts before 10.20.0.10 -
        a plain string sort would put "10" before "9" and get that
        backwards. Non-IP values (blank, "-") sort after real IPs.
        """
        try:
            return (0, ipaddress.ip_address(value))
        except (ValueError, TypeError):
            return (1, str(value).lower())

    def _enable_sorting(self, tree, key_funcs: dict):
        """Wire each column's heading to sort that Treeview by it,
        toggling ascending/descending on repeated clicks.
        """
        if not hasattr(self, "_sort_reverse"):
            self._sort_reverse = {}
        for col, key_func in key_funcs.items():
            tree.heading(col, command=lambda c=col, kf=key_func: self._sort_tree(tree, c, kf))

    def _sort_tree(self, tree, col, key_func):
        state_key = (str(tree), col)
        reverse = self._sort_reverse.get(state_key, False)
        rows = [(tree.set(iid, col), iid) for iid in tree.get_children("")]
        rows.sort(key=lambda pair: key_func(pair[0]), reverse=reverse)
        for index, (_, iid) in enumerate(rows):
            tree.move(iid, "", index)
        self._sort_reverse[state_key] = not reverse


class _QueueWriter:
    """A minimal stdout-like object: write() pushes text onto a queue
    instead of a real stream. Used to capture orchestrator.py's
    existing print() calls during a background discovery run and
    forward them to the GUI's console panel, without touching
    orchestrator.py itself at all.
    """

    def __init__(self, output_queue: queue.Queue):
        self._queue = output_queue

    def write(self, text: str) -> None:
        if text:
            self._queue.put(text)

    def flush(self) -> None:
        pass


class _Tooltip:
    """A single small borderless popup shared by every worker-status
    cell in DiscoveryTab, showing that cell's full (un-abbreviated)
    status message on hover. One shared Toplevel rather than one per
    cell - up to a dozen-plus popup windows sitting around, each
    created/destroyed as the mouse moves, is unnecessary churn for
    something this simple; show()/hide() just move and re-show the one
    instance.
    """

    def __init__(self, owner: tk.Widget):
        self._owner = owner
        self._win = None

    def show(self, widget: tk.Widget, text: str) -> None:
        if not text:
            return
        self.hide()
        x = widget.winfo_rootx() + 12
        y = widget.winfo_rooty() + widget.winfo_height() + 6
        self._win = tk.Toplevel(self._owner)
        self._win.wm_overrideredirect(True)
        self._win.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._win, text=text, background="#ffffe0", foreground="#000000",
            relief="solid", borderwidth=1, padx=6, pady=3, font=("Segoe UI", 9), justify="left",
        ).pack()

    def hide(self) -> None:
        if self._win is not None:
            self._win.destroy()
            self._win = None


class LoginDialog(tk.Toplevel):
    """Mandatory, no-bypass login gate shown once at app launch, before
    the main window is built at all - every session is tied to a known
    identity from the start (for the activity log - see
    db.log_activity()), rather than only being prompted for credentials
    the first time some tab happens to need the network.
    Closing this window (the X button) exits the whole app - the only
    two ways out are a successful unlock/store-creation, or the
    explicit Exit button, which also exits.

    Handles both real cases:
      - An existing credential store for this OS user - prompts for
        the master password, offers a "create a new personal store
        instead" escape hatch after 2 failed attempts (mirroring
        credential_manager.py's own unlock_store(), for the same
        reason: the resolved file might genuinely belong to someone
        else on a shared drive, not necessarily be a wrong password
        for your own store).
      - No store yet for this OS user - walks through creating one
        right here, rather than pointing at a separate CLI tool.

    On success: sets db.CURRENT_USER/db.CURRENT_CREDENTIAL_STORE, logs
    the login via db.log_activity(), and sets self.result to (path,
    decrypted credentials dict) - the caller (App) hands this same
    dict to every tab that needs it, so nothing prompts again later.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Site Awareness Dashboard - Login")
        self.result = None
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_exit)

        try:
            self._os_user = getpass.getuser()
        except Exception:
            self._os_user = None
        db.CURRENT_USER = self._os_user

        self._path = credential_store.resolve_credentials_path()
        self._store_exists = os.path.exists(self._path)
        self._failed_attempts = 0
        self._master_password = None

        self._build_layout()
        self.wait_window(self)

    def _build_layout(self):
        for widget in self.winfo_children():
            widget.destroy()

        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)

        if self._store_exists:
            self._build_unlock_form(container)
        else:
            self._build_create_form(container)

    def _build_unlock_form(self, container):
        ttk.Label(
            container, text=f"Unlock credential store:\n{self._path}", justify="left",
        ).pack(anchor="w", pady=(0, 10))
        ttk.Label(container, text="Master password:").pack(anchor="w")
        self.pw_entry = ttk.Entry(container, show="*", width=36)
        self.pw_entry.pack(pady=(0, 4))
        self.pw_entry.focus_set()
        self.pw_entry.bind("<Return>", lambda e: self._on_unlock())

        self.error_label = ttk.Label(container, text="", foreground="#990000")
        self.error_label.pack(anchor="w", pady=(0, 8))

        button_row = ttk.Frame(container)
        button_row.pack(fill="x")
        ttk.Button(button_row, text="Exit", command=self._on_exit).pack(side="right", padx=(6, 0))
        ttk.Button(button_row, text="Unlock", command=self._on_unlock).pack(side="right")

    def _build_create_form(self, container):
        ttk.Label(
            container,
            text=f"No credential store found for this account yet.\nA new one will be created at:\n{self._path}",
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        ttk.Label(container, text="New master password:").pack(anchor="w")
        self.pw_entry = ttk.Entry(container, show="*", width=36)
        self.pw_entry.pack(pady=(0, 4))
        self.pw_entry.focus_set()

        ttk.Label(container, text="Verify master password:").pack(anchor="w")
        self.pw_verify_entry = ttk.Entry(container, show="*", width=36)
        self.pw_verify_entry.pack(pady=(0, 4))
        self.pw_verify_entry.bind("<Return>", lambda e: self._on_create())

        self.error_label = ttk.Label(container, text="", foreground="#990000")
        self.error_label.pack(anchor="w", pady=(0, 8))

        button_row = ttk.Frame(container)
        button_row.pack(fill="x")
        ttk.Button(button_row, text="Exit", command=self._on_exit).pack(side="right", padx=(6, 0))
        ttk.Button(button_row, text="Create", command=self._on_create).pack(side="right")

    def _on_unlock(self):
        mp = self.pw_entry.get()
        try:
            creds = credential_store.load_and_decrypt(self._path, mp)
        except InvalidTag:
            self._failed_attempts += 1
            db.log_activity("login_failed", f"invalid password for {self._path}")
            if self._failed_attempts >= 2:
                if messagebox.askyesno(
                    "Wrong store?",
                    f"'{self._path}' might not be your store (e.g. it belongs to someone else "
                    f"on a shared drive). Create your own new personal store instead?",
                ):
                    try:
                        self._path = f"credentials_{getpass.getuser()}.enc"
                    except Exception:
                        self._path = credential_store.DEFAULT_CREDENTIALS_FILE
                    self._store_exists = os.path.exists(self._path)
                    self._failed_attempts = 0
                    self._build_layout()
                    return
            self.error_label.configure(text="Invalid password or corrupted file. Try again.")
            self.pw_entry.delete(0, "end")
            return
        except Exception as e:
            self.error_label.configure(text=f"Could not open credential store: {e}")
            return

        self._master_password = mp
        self._finish_login(creds)

    def _on_create(self):
        mp = self.pw_entry.get()
        mp_verify = self.pw_verify_entry.get()
        if not mp:
            self.error_label.configure(text="Master password cannot be blank.")
            return
        if mp != mp_verify:
            self.error_label.configure(text="Passwords do not match.")
            self.pw_verify_entry.delete(0, "end")
            return

        try:
            credentials = {}
            credential_store.save_and_encrypt(self._path, credentials, mp)
        except Exception as e:
            self.error_label.configure(text=f"Could not create credential store: {e}")
            return

        # Matches credential_manager.py's own initialize_store(): offer
        # to set up the one credential everyone needs right away,
        # rather than making a first-time user hunt for it later.
        self._new_store_password = mp
        self._new_store_credentials = credentials
        self._build_tacacs_setup_form()

    def _build_tacacs_setup_form(self):
        for widget in self.winfo_children():
            widget.destroy()

        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text="Set up your 'tacacs' credential now?\nThis is what SAD uses to log into network devices.",
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        ttk.Label(container, text="Username:").pack(anchor="w")
        self.tacacs_user_entry = ttk.Entry(container, width=36)
        self.tacacs_user_entry.pack(pady=(0, 4))
        self.tacacs_user_entry.focus_set()

        ttk.Label(container, text="Password:").pack(anchor="w")
        self.tacacs_pass_entry = ttk.Entry(container, show="*", width=36)
        self.tacacs_pass_entry.pack(pady=(0, 4))
        self.tacacs_pass_entry.bind("<Return>", lambda e: self._on_tacacs_save())

        self.error_label = ttk.Label(container, text="", foreground="#990000")
        self.error_label.pack(anchor="w", pady=(0, 8))

        button_row = ttk.Frame(container)
        button_row.pack(fill="x")
        ttk.Button(button_row, text="Skip", command=self._on_tacacs_skip).pack(side="right", padx=(6, 0))
        ttk.Button(button_row, text="Save", command=self._on_tacacs_save).pack(side="right")

    def _on_tacacs_save(self):
        username = self.tacacs_user_entry.get().strip()
        password = self.tacacs_pass_entry.get()
        if not username or not password:
            self.error_label.configure(text="Both username and password are required (or click Skip).")
            return

        self._new_store_credentials["tacacs"] = {
            "username": {"value": username, "sensitive": False},
            "password": {"value": password, "sensitive": True},
        }
        try:
            credential_store.save_and_encrypt(self._path, self._new_store_credentials, self._new_store_password)
        except Exception as e:
            self.error_label.configure(text=f"Could not save the 'tacacs' credential: {e}")
            return

        self._master_password = self._new_store_password
        self._finish_login(self._new_store_credentials)

    def _on_tacacs_skip(self):
        self._master_password = self._new_store_password
        self._finish_login(self._new_store_credentials)

    def _finish_login(self, credentials):
        db.CURRENT_CREDENTIAL_STORE = self._path
        db.log_activity("login", f"unlocked {self._path}")
        self.result = (self._path, credentials, self._master_password)
        self.destroy()

    def _on_exit(self):
        db.log_activity("login_cancelled", "user exited at the login screen")
        self.destroy()
        sys.exit(0)


class DiscoveryTab(ttk.Frame):
    """GUI over orchestrator.py's CDP/ARP collection. Scope (one site
    or all) and action (cdp/arp/both) pickers up top, a Run button, an
    overall progress bar, one status column per active worker thread
    (color-coded by phase, with a hover tooltip for the full detail
    text and a highlighted border if a worker's had no update in a
    while), and a scrolling read-only console below showing live
    output from the run - which happens on a background thread so the
    window stays responsive during a multi-minute discovery pass.
    """

    def __init__(self, parent, creds):
        super().__init__(parent)
        self._creds = creds
        self._worker_thread = None
        self._output_queue = queue.Queue()
        self._progress_queue = queue.Queue()
        self._progress_total = 0
        self._progress_done = 0
        self._site_keys = []
        self._build_layout()
        self._refresh_site_choices()
        self._poll_queue()
        self._poll_progress_queue()
        self._check_worker_staleness()

    def _build_layout(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        controls = ttk.Frame(self, padding=10)
        controls.grid(row=0, column=0, sticky="ew")

        ttk.Label(controls, text="Scope").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.scope_var = tk.StringVar(value="site")
        ttk.Radiobutton(
            controls, text="One site:", variable=self.scope_var, value="site", command=self._on_scope_changed,
        ).grid(row=0, column=1, sticky="w")
        self.site_combo = ttk.Combobox(controls, state="readonly", width=28)
        self.site_combo.grid(row=0, column=2, sticky="w", padx=(4, 20))
        ttk.Radiobutton(
            controls, text="All sites", variable=self.scope_var, value="all", command=self._on_scope_changed,
        ).grid(row=0, column=3, sticky="w")

        ttk.Label(controls, text="Action").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        self.action_var = tk.StringVar(value="cdp")
        ttk.Radiobutton(controls, text="CDP discovery", variable=self.action_var, value="cdp",
                        command=self._on_action_changed).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Radiobutton(controls, text="ARP collection", variable=self.action_var, value="arp",
                        command=self._on_action_changed).grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Radiobutton(controls, text="Both", variable=self.action_var, value="all",
                        command=self._on_action_changed).grid(row=1, column=3, sticky="w", pady=(8, 0))
        ttk.Radiobutton(controls, text="Generate dashboard", variable=self.action_var, value="dashboard",
                        command=self._on_action_changed).grid(row=1, column=4, sticky="w", pady=(8, 0))

        self.collect_mac_var = tk.BooleanVar(value=False)
        self.collect_mac_check = ttk.Checkbutton(
            controls, text="Also collect MAC address tables (with CDP discovery)",
            variable=self.collect_mac_var,
        )
        self.collect_mac_check.grid(row=2, column=1, columnspan=4, sticky="w", pady=(4, 0))

        self.run_button = ttk.Button(controls, text="Run", command=self._on_run_clicked)
        self.run_button.grid(row=0, column=5, rowspan=2, sticky="ns", padx=(20, 0))

        # Overall progress: how many of the sites in this run have
        # finished (successfully or not) - one determinate step per
        # site, incremented once each site's _run_site_actions()
        # completes (see run_for_sites()'s progress_cb "done"/"error"
        # events), not per individual device/action within a site.
        progress_frame = ttk.Frame(self, padding=(10, 6, 10, 0))
        progress_frame.grid(row=1, column=0, sticky="ew")
        progress_frame.columnconfigure(0, weight=1)

        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=1, value=0)
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_label = ttk.Label(progress_frame, text="")
        self.progress_label.grid(row=0, column=1, sticky="w", padx=(10, 0))

        # Per-worker status: one column per concurrently-running worker
        # slot (not per site - with up to DISCOVERY_MAX_WORKERS threads
        # against a couple hundred sites, a per-site table would be
        # mostly "Queued" rows and would dwarf the window). Each column
        # shows whichever site that worker thread is currently on and
        # an abbreviated phase word, color-coded, with the full detail
        # message available on hover. Built fresh (see
        # _init_worker_strip()) at the start of each run, sized to
        # however many workers this specific run can actually use.
        ttk.Label(self, text="Thread status").grid(row=2, column=0, sticky="w", padx=10, pady=(6, 0))
        self.worker_strip = ttk.Frame(self, padding=(10, 2, 10, 0))
        self.worker_strip.grid(row=3, column=0, sticky="ew")
        self._worker_cells = []       # list of dicts, index = slot - 1
        self._worker_last_seen = {}   # slot -> time.monotonic() of its last update
        self._worker_phase = {}       # slot -> current phase string
        self._tooltip = _Tooltip(self)

        ttk.Label(self, text="Console").grid(row=4, column=0, sticky="w", padx=10, pady=(6, 0))

        console_frame = ttk.Frame(self, padding=(10, 4, 10, 10))
        console_frame.grid(row=5, column=0, sticky="nsew")
        console_frame.rowconfigure(0, weight=1)
        console_frame.columnconfigure(0, weight=1)

        self.console = scrolledtext.ScrolledText(
            console_frame, height=10, state="disabled", wrap="word",
            background="#111111", foreground="#dddddd", insertbackground="#dddddd", font=("Consolas", 10),
        )
        self.console.grid(row=0, column=0, sticky="nsew")

        self._on_scope_changed()
        self._on_action_changed()

    def _on_scope_changed(self):
        self.site_combo.configure(state="readonly" if self.scope_var.get() == "site" else "disabled")

    def _on_action_changed(self):
        # MAC-table collection piggybacks on the CDP walk specifically
        # (see discover_site()'s collect_mac_tables option) - only
        # meaningful when CDP is actually part of what's about to run.
        collect_mac_relevant = self.action_var.get() in ("cdp", "all")
        self.collect_mac_check.configure(state="normal" if collect_mac_relevant else "disabled")

    def _refresh_site_choices(self):
        with db.get_conn() as conn:
            sites = db.get_all_sites(conn)
        self._site_keys = [site["id"] for site in sites]
        display_values = [f"{site['site_name'] or site['site_octet']} ({site['site_octet']})" for site in sites]
        self.site_combo["values"] = display_values
        if display_values:
            self.site_combo.current(0)

    def _append_console(self, text: str):
        self.console.configure(state="normal")
        self.console.insert("end", text)
        self.console.see("end")
        self.console.configure(state="disabled")

    def _clear_console(self):
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                item = self._output_queue.get_nowait()
                if item is _DONE:
                    self.run_button.configure(state="normal")
                else:
                    self._append_console(item)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _reset_progress_display(self):
        self._progress_total = 0
        self._progress_done = 0
        self.progress_bar.configure(maximum=1, value=0)
        self.progress_label.configure(text="")
        self._init_worker_strip(0)

    def _init_worker_strip(self, worker_count: int):
        # Rebuilt fresh each run, sized to however many workers THIS
        # run can actually use (min(DISCOVERY_MAX_WORKERS, site count) -
        # computed by _run_worker() before this fires) - a single-site
        # run gets one column, not a row of empty ones.
        for cell in self._worker_cells:
            cell["frame"].destroy()
        self._worker_cells = []
        self._worker_last_seen = {}
        self._worker_phase = {}
        for i in range(worker_count):
            slot = i + 1
            bg, fg, label = PHASE_STYLES["idle"]
            cell_frame = tk.Frame(self.worker_strip, background=bg, highlightthickness=2,
                                   highlightbackground=bg, borderwidth=1, relief="solid")
            cell_frame.grid(row=0, column=i, sticky="nsew", padx=1, pady=1)
            self.worker_strip.columnconfigure(i, weight=1, uniform="worker")
            header = tk.Label(cell_frame, text=f"W{slot}", background=bg, foreground="#555555",
                               font=("Segoe UI", 8, "bold"))
            header.pack(fill="x", padx=4, pady=(3, 0))
            site_label = tk.Label(cell_frame, text="", background=bg, foreground=fg,
                                   font=("Segoe UI", 9, "bold"), wraplength=90, justify="left")
            site_label.pack(fill="x", padx=4)
            status_label = tk.Label(cell_frame, text=label, background=bg, foreground=fg,
                                     font=("Segoe UI", 8), wraplength=90, justify="left")
            status_label.pack(fill="x", padx=4, pady=(0, 4))

            cell = {
                "frame": cell_frame, "header": header, "site_label": site_label,
                "status_label": status_label, "site_octet": None, "message": "",
            }
            self._worker_cells.append(cell)
            self._worker_phase[slot] = "idle"

            for widget in (cell_frame, header, site_label, status_label):
                widget.bind("<Enter>", lambda e, s=slot: self._on_worker_hover(s, e.widget))
                widget.bind("<Leave>", lambda e: self._tooltip.hide())

    def _set_worker_cell(self, slot: int, phase: str, site_octet: str = None, message: str = ""):
        if slot is None or slot < 1 or slot > len(self._worker_cells):
            return
        cell = self._worker_cells[slot - 1]
        bg, fg, label = PHASE_STYLES.get(phase, PHASE_STYLES["walking"])
        cell["frame"].configure(background=bg, highlightbackground=bg)
        cell["header"].configure(background=bg)
        cell["site_label"].configure(background=bg, foreground=fg, text=site_octet or "")
        cell["status_label"].configure(background=bg, foreground=fg, text=label)
        cell["site_octet"] = site_octet
        cell["message"] = message or label
        self._worker_phase[slot] = phase
        self._worker_last_seen[slot] = time.monotonic()

    def _on_worker_hover(self, slot: int, widget: tk.Widget):
        if slot < 1 or slot > len(self._worker_cells):
            return
        cell = self._worker_cells[slot - 1]
        octet = cell["site_octet"]
        text = f"Site {octet}\n{cell['message']}" if octet else cell["message"]
        self._tooltip.show(widget, text)

    def _check_worker_staleness(self):
        now = time.monotonic()
        for slot, cell in enumerate(self._worker_cells, start=1):
            phase = self._worker_phase.get(slot)
            last_seen = self._worker_last_seen.get(slot)
            if phase in _TERMINAL_PHASES or last_seen is None:
                continue
            stuck = (now - last_seen) >= STALE_WORKER_SECONDS
            # A thick red highlight border layered on the cell's normal
            # phase color - deliberately NOT replacing that color, so
            # you can still see what it was doing when it stalled, not
            # just that something's wrong.
            cell["frame"].configure(highlightbackground="#c0392b" if stuck else cell["frame"]["background"])
        self.after(STALE_CHECK_INTERVAL_MS, self._check_worker_staleness)

    def _advance_progress(self):
        self._progress_done += 1
        self.progress_bar.configure(value=self._progress_done)
        self.progress_label.configure(text=f"{self._progress_done} / {self._progress_total} sites complete")

    def _on_discovery_progress(self, event: str, site_octet: str, slot, phase: str, message: str = ""):
        # Called directly from run_for_sites()'s worker threads - one
        # per concurrently-scanning site - so this may run from several
        # threads at once. queue.Queue.put() is thread-safe on its own;
        # nothing here touches a Tkinter widget directly, only
        # _poll_progress_queue() (on the main thread, via after()) does
        # that, exactly like the existing console output pattern above.
        self._progress_queue.put({
            "event": event, "site": site_octet, "slot": slot, "phase": phase, "message": message,
        })

    def _poll_progress_queue(self):
        try:
            while True:
                item = self._progress_queue.get_nowait()
                event = item["event"]
                if event == "init":
                    self._progress_total = item["total_sites"]
                    self._progress_done = 0
                    self.progress_bar.configure(maximum=max(self._progress_total, 1), value=0)
                    self.progress_label.configure(text=f"0 / {self._progress_total} sites complete")
                    self._init_worker_strip(item["workers"])
                elif event == "status":
                    self._set_worker_cell(item["slot"], item["phase"], item["site"], item["message"])
                elif event == "done":
                    # run_for_sites()'s own "done" event carries no
                    # message by design - the site's last action
                    # already left a specific, informative status (e.g.
                    # "CDP walk complete: 4 device(s), 3 link(s)") via
                    # its own "status" events; overwriting that with a
                    # bare "Done" would throw away the one detail worth
                    # keeping on hover, so only the phase/color changes.
                    slot = item["slot"]
                    if slot is not None and 1 <= slot <= len(self._worker_cells):
                        cell = self._worker_cells[slot - 1]
                        self._set_worker_cell(slot, "done", cell["site_octet"], cell["message"])
                    self._advance_progress()
                elif event == "error":
                    self._set_worker_cell(item["slot"], "error", item["site"], item["message"])
                    self._advance_progress()
        except queue.Empty:
            pass
        self.after(100, self._poll_progress_queue)

    def _ensure_tacacs_available(self) -> bool:
        """The credential store is guaranteed unlocked (login succeeded
        before this tab was ever built) - this just confirms the
        'tacacs' type specifically has been added to it, which isn't
        guaranteed just because the store itself exists and unlocked.
        """
        try:
            orchestrator._get_tacacs_credentials(self._creds)
        except ValueError as e:
            messagebox.showerror("Credentials", str(e))
            return False
        return True

    def _on_run_clicked(self):
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return  # defensive - the button should already be disabled during a run

        if self.scope_var.get() == "site" and self.site_combo.current() < 0:
            messagebox.showwarning("Discovery", "Choose a site first, or select 'All sites'.")
            return

        action = self.action_var.get()

        # Dashboard generation never touches the network - no reason to
        # validate credentials it doesn't need.
        creds = None
        if action != "dashboard":
            if not self._ensure_tacacs_available():
                return
            creds = self._creds

        site_id = None
        if self.scope_var.get() == "site":
            site_id = self._site_keys[self.site_combo.current()]

        self._clear_console()
        self._reset_progress_display()
        self.run_button.configure(state="disabled")

        if action == "dashboard":
            worker_target, worker_args = self._run_dashboard_worker, (site_id,)
        else:
            actions = ["cdp", "arp"] if action == "all" else [action]
            collect_mac_tables = self.collect_mac_var.get() and action in ("cdp", "all")
            worker_target, worker_args = self._run_worker, (site_id, actions, creds, collect_mac_tables)

        self._worker_thread = threading.Thread(target=worker_target, args=worker_args, daemon=True)
        self._worker_thread.start()

    def _run_dashboard_worker(self, site_id):
        original_stdout = sys.stdout
        sys.stdout = _QueueWriter(self._output_queue)
        try:
            scope_label = "all sites" if site_id is None else f"site id {site_id}"
            db.log_activity("generate_dashboard", f"Generate dashboard - {scope_label}")
            if site_id is None:
                dashboard_generate.generate_all()
            else:
                with db.get_conn() as conn:
                    # Looked up by primary key, same reasoning as
                    # _run_worker below - unambiguous, unlike a
                    # string-key lookup.
                    site = db.get_site_by_id(conn, site_id)
                    if site is None:
                        print(f"Selected site (id {site_id}) no longer exists - try refreshing.")
                    else:
                        # generate_site()/generate_index() are silent by
                        # design (only the CLI-facing generate_all()/
                        # generate_one() wrappers print) - print here so
                        # the console shows something happened.
                        path = dashboard_generate.generate_site(conn, site)
                        print(f"  Wrote {path}")
                        # Regenerating one site's page should also
                        # refresh the index, since it shows aggregate
                        # counts/timestamps that would otherwise go
                        # stale the moment this site's data changes.
                        index_path = dashboard_generate.generate_index(conn)
                        print(f"  Wrote {index_path}")
        except Exception as e:
            print(f"ERROR: {e}")
        finally:
            sys.stdout = original_stdout
            self._output_queue.put(_DONE)

    def _run_worker(self, site_id, actions, creds, collect_mac_tables=False):
        original_stdout = sys.stdout
        sys.stdout = _QueueWriter(self._output_queue)
        try:
            with db.get_conn() as conn:
                if site_id is None:
                    # Excludes "Unassigned" - it never has a seed
                    # device, so including it in an "All sites" scan is
                    # pure overhead (picked up, immediately reports "no
                    # seed device flagged", moves on).
                    sites = db.get_all_sites(conn, include_unassigned=False)
                else:
                    # Looked up by primary key, not by octet/name/code
                    # string - unambiguous by construction, unlike
                    # find_site_by_any_key(), which risks matching the
                    # wrong site if some other site's name/code happens
                    # to coincidentally equal this one's octet.
                    site = db.get_site_by_id(conn, site_id)
                    sites = [site] if site is not None else []

            if not sites:
                if site_id is None:
                    print("No sites found in the database.")
                else:
                    print(f"Selected site (id {site_id}) no longer exists - try refreshing.")
            else:
                # Seeds the progress bar and builds one worker-status
                # column per worker this run can actually use, before
                # any worker thread actually starts - min() because a
                # single-site (or otherwise small) run never uses more
                # workers than it has sites for, and a wall of idle
                # columns that will never light up isn't useful.
                effective_workers = min(orchestrator.DISCOVERY_MAX_WORKERS, len(sites))
                self._progress_queue.put({
                    "event": "init", "total_sites": len(sites), "workers": effective_workers,
                })
                # No conn passed - run_for_sites() scans multiple sites
                # concurrently, each site's own reads/writes managed
                # independently (short-lived reads, queued writes), so
                # no single connection/transaction is ever held open
                # for the whole scan.
                orchestrator.run_for_sites(
                    sites, actions, creds, collect_mac_tables=collect_mac_tables,
                    progress_cb=self._on_discovery_progress,
                )
        except Exception as e:
            print(f"ERROR: {e}")
        finally:
            # Final drain pass: every write during the scan already
            # went through queue_and_wait() (each worker thread blocks
            # until its own write is actually applied), so this isn't
            # needed for correctness - but it's cheap insurance against
            # anything left behind by, say, a thread that raised
            # partway through, or another process's items that arrived
            # during the scan. Without it, those would just sit until
            # this GUI's next periodic timer tick (up to DRAIN_INTERVAL_MS
            # away) instead of clearing immediately once the scan ends.
            try:
                write_queue.try_drain()
            except Exception as e:
                print(f"Warning: final queue drain failed: {e}")
            sys.stdout = original_stdout
            self._output_queue.put(_DONE)


class ExportTab(ttk.Frame):
    """GUI over csv_export.py. Two independent checkboxes (you might
    want both at once, so these are checkboxes, not a mutually-
    exclusive radio choice like Discovery's Action picker) plus a
    sub-choice for the phone/VTC export specifically, a Run button,
    and the same background-thread/live-console pattern DiscoveryTab
    already established. No credential prompt at all - csv_export.py
    only ever reads from the local database, it never touches the
    network.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self._worker_thread = None
        self._output_queue = queue.Queue()
        self._build_layout()
        self._poll_queue()

    def _build_layout(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        controls = ttk.Frame(self, padding=10)
        controls.grid(row=0, column=0, sticky="ew")

        self.phones_vtc_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls, text="Phone / VTC inventory", variable=self.phones_vtc_var,
            command=self._on_phones_vtc_changed,
        ).grid(row=0, column=0, sticky="w")

        self.phone_vtc_scope_var = tk.StringVar(value="both")
        self.scope_both_radio = ttk.Radiobutton(
            controls, text="Both", variable=self.phone_vtc_scope_var, value="both",
        )
        self.scope_both_radio.grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.scope_phones_radio = ttk.Radiobutton(
            controls, text="Phones only", variable=self.phone_vtc_scope_var, value="phones",
        )
        self.scope_phones_radio.grid(row=0, column=2, sticky="w")
        self.scope_vtc_radio = ttk.Radiobutton(
            controls, text="VTCs only", variable=self.phone_vtc_scope_var, value="vtc",
        )
        self.scope_vtc_radio.grid(row=0, column=3, sticky="w")

        self.devices_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Device inventory", variable=self.devices_var).grid(
            row=1, column=0, sticky="w", pady=(6, 0))

        self.run_button = ttk.Button(controls, text="Run", command=self._on_run_clicked)
        self.run_button.grid(row=0, column=5, rowspan=2, sticky="ns", padx=(20, 0))

        ttk.Label(self, text="Console").grid(row=1, column=0, sticky="w", padx=10, pady=(6, 0))

        console_frame = ttk.Frame(self, padding=(10, 4, 10, 10))
        console_frame.grid(row=2, column=0, sticky="nsew")
        console_frame.rowconfigure(0, weight=1)
        console_frame.columnconfigure(0, weight=1)

        self.console = scrolledtext.ScrolledText(
            console_frame, height=16, state="disabled", wrap="word",
            background="#111111", foreground="#dddddd", insertbackground="#dddddd", font=("Consolas", 10),
        )
        self.console.grid(row=0, column=0, sticky="nsew")

        self._on_phones_vtc_changed()

    def _on_phones_vtc_changed(self):
        state = "normal" if self.phones_vtc_var.get() else "disabled"
        self.scope_both_radio.configure(state=state)
        self.scope_phones_radio.configure(state=state)
        self.scope_vtc_radio.configure(state=state)

    def _append_console(self, text: str):
        self.console.configure(state="normal")
        self.console.insert("end", text)
        self.console.see("end")
        self.console.configure(state="disabled")

    def _clear_console(self):
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                item = self._output_queue.get_nowait()
                if item is _DONE:
                    self.run_button.configure(state="normal")
                else:
                    self._append_console(item)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_run_clicked(self):
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return  # defensive - the button should already be disabled during a run

        run_phones_vtc = self.phones_vtc_var.get()
        run_devices = self.devices_var.get()
        if not run_phones_vtc and not run_devices:
            messagebox.showwarning("Export", "Choose at least one export to run.")
            return

        scope = self.phone_vtc_scope_var.get()
        include_phones = scope in ("both", "phones")
        include_vtcs = scope in ("both", "vtc")

        self._clear_console()
        self.run_button.configure(state="disabled")

        self._worker_thread = threading.Thread(
            target=self._run_worker,
            args=(run_phones_vtc, include_phones, include_vtcs, run_devices),
            daemon=True,
        )
        self._worker_thread.start()

    def _run_worker(self, run_phones_vtc, include_phones, include_vtcs, run_devices):
        original_stdout = sys.stdout
        sys.stdout = _QueueWriter(self._output_queue)
        try:
            with db.get_conn() as conn:
                if run_phones_vtc:
                    path = csv_export.export_phone_vtc_inventory(conn, include_phones, include_vtcs)
                    print(f"Wrote {path}")
                if run_devices:
                    path = csv_export.export_device_inventory(conn)
                    print(f"Wrote {path}")
            exports_path = dashboard_generate.generate_exports_page()
            print(f"Wrote {exports_path}")
        except Exception as e:
            print(f"ERROR: {e}")
        finally:
            sys.stdout = original_stdout
            self._output_queue.put(_DONE)


class CommandRunnerTab(ttk.Frame):
    """GUI over orchestrator.run_plugin() - SAD's first write-capable
    feature. The central safety mechanism: Commit is disabled until a
    Preview (always dry-run) has been run for the EXACT current scope/
    plugin/parameter combination - any change to any of those after a
    preview immediately disables Commit again, forcing a fresh preview
    before anything can actually be pushed. Preview and Commit are
    deliberately two separate, distinctly-labeled buttons rather than
    one Run button plus a commit checkbox, which would be too easy to
    leave checked between unrelated runs.
    """

    def __init__(self, parent, creds):
        super().__init__(parent)
        self._creds = creds
        self._worker_thread = None
        self._output_queue = queue.Queue()
        self._site_keys = []
        self._param_vars = {}
        self._last_previewed_signature = None
        self._pending_completion = None
        self._plugins = orchestrator.list_plugins()
        self._plugin_ids = sorted(self._plugins)
        self._build_layout()
        self._poll_queue()
        self._refresh_site_choices()

    def _build_layout(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        controls = ttk.Frame(self, padding=10)
        controls.grid(row=0, column=0, sticky="ew")

        ttk.Label(controls, text="Scope").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.scope_var = tk.StringVar(value="site")
        scope_radio_site = ttk.Radiobutton(
            controls, text="One site:", variable=self.scope_var, value="site",
            command=self._invalidate_preview,
        )
        scope_radio_site.grid(row=0, column=1, sticky="w")
        self.site_combo = ttk.Combobox(controls, state="readonly", width=28)
        self.site_combo.grid(row=0, column=2, sticky="w", padx=(4, 20))
        self.site_combo.bind("<<ComboboxSelected>>", lambda e: self._invalidate_preview())
        scope_radio_all = ttk.Radiobutton(
            controls, text="All sites", variable=self.scope_var, value="all",
            command=self._invalidate_preview,
        )
        scope_radio_all.grid(row=0, column=3, sticky="w")
        self.scope_row_widgets = [scope_radio_site, self.site_combo, scope_radio_all]
        self.scope_note_label = ttk.Label(controls, text="")
        self.scope_note_label.grid(row=0, column=4, sticky="w", padx=(10, 0))

        ttk.Label(controls, text="Script").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        self.plugin_combo = ttk.Combobox(controls, state="readonly", width=40)
        self.plugin_combo["values"] = [self._plugins[pid].NAME for pid in self._plugin_ids]
        self.plugin_combo.grid(row=1, column=1, columnspan=3, sticky="w", pady=(8, 0))
        self.plugin_combo.bind("<<ComboboxSelected>>", lambda e: self._on_plugin_changed())

        self.plugin_desc_label = ttk.Label(controls, text="", foreground="#666666", wraplength=520)
        self.plugin_desc_label.grid(row=2, column=1, columnspan=4, sticky="w")

        self.param_frame = ttk.Frame(controls)
        self.param_frame.grid(row=3, column=1, columnspan=4, sticky="w", pady=(6, 0))

        self.save_var = tk.BooleanVar(value=False)
        self.save_checkbox = ttk.Checkbutton(
            controls, text="Save config after committing", variable=self.save_var,
        )
        self.save_checkbox.grid(row=4, column=1, columnspan=4, sticky="w", pady=(6, 0))

        button_frame = ttk.Frame(controls)
        button_frame.grid(row=0, column=5, rowspan=5, sticky="ns", padx=(20, 0))
        self.preview_button = ttk.Button(button_frame, text="Preview (dry-run)", command=self._on_preview_clicked)
        self.preview_button.pack(fill="x", pady=(0, 6))
        self.commit_button = ttk.Button(
            button_frame, text="Commit (apply changes)", command=self._on_commit_clicked, state="disabled",
        )
        self.commit_button.pack(fill="x")

        ttk.Label(self, text="Console").grid(row=1, column=0, sticky="w", padx=10, pady=(6, 0))
        self.preview_status_label = ttk.Label(self, text="Not yet previewed for the current settings.")
        self.preview_status_label.grid(row=2, column=0, sticky="w", padx=10)

        console_frame = ttk.Frame(self, padding=(10, 4, 10, 10))
        console_frame.grid(row=3, column=0, sticky="nsew")
        console_frame.rowconfigure(0, weight=1)
        console_frame.columnconfigure(0, weight=1)
        self.console = scrolledtext.ScrolledText(
            console_frame, height=16, state="disabled", wrap="word",
            background="#111111", foreground="#dddddd", insertbackground="#dddddd", font=("Consolas", 10),
        )
        self.console.grid(row=0, column=0, sticky="nsew")

        self._invalidate_preview()
        if self.plugin_combo["values"]:
            self.plugin_combo.current(0)
            self._on_plugin_changed()

    def _selected_plugin_id(self):
        idx = self.plugin_combo.current()
        return self._plugin_ids[idx] if idx >= 0 else None

    def _on_plugin_changed(self):
        for widget in self.param_frame.winfo_children():
            widget.destroy()
        self._param_vars = {}

        plugin_id = self._selected_plugin_id()
        if plugin_id is not None:
            module = self._plugins[plugin_id]
            self.plugin_desc_label.configure(text=module.DESCRIPTION)
            for i, param in enumerate(module.PARAMS):
                ttk.Label(self.param_frame, text=param["label"]).grid(
                    row=i, column=0, sticky="w", padx=(0, 8), pady=2)
                var = tk.StringVar()
                var.trace_add("write", lambda *a: self._invalidate_preview())
                ttk.Entry(self.param_frame, textvariable=var, width=30).grid(row=i, column=1, sticky="w", pady=2)
                self._param_vars[param["name"]] = (var, param.get("required", True))
        else:
            self.plugin_desc_label.configure(text="")

        self._update_plugin_shape_widgets()
        self._invalidate_preview()

    def _is_standalone_plugin_selected(self) -> bool:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return False
        return orchestrator.is_standalone_plugin(self._plugins[plugin_id])

    def _update_plugin_shape_widgets(self):
        """Standalone plugins own their entire device list - scope has
        no meaning for them, so the scope controls are disabled and a
        note explains why, rather than leaving a scope picker on
        screen that this kind of script simply ignores. Saving is
        similarly outside the harness's control for this shape:
        run_standalone_plugin() doesn't even accept a save argument -
        whether a standalone plugin saves config (and when) is
        entirely up to its own run() logic, so leaving the checkbox
        enabled would be actively misleading, not just irrelevant.
        """
        if self._is_standalone_plugin_selected():
            for child in self.scope_row_widgets:
                child.configure(state="disabled")
            self.scope_note_label.configure(text="This script manages its own device list.")
            self.save_checkbox.configure(state="disabled")
        else:
            for child in self.scope_row_widgets:
                child.configure(state="normal")
            self.site_combo.configure(state="readonly" if self.scope_var.get() == "site" else "disabled")
            self.scope_note_label.configure(text="")
            self.save_checkbox.configure(state="normal")

    def _current_params(self):
        return {name: var.get().strip() for name, (var, _required) in self._param_vars.items()}

    def _missing_required_params(self):
        return [name for name, (var, required) in self._param_vars.items() if required and not var.get().strip()]

    def _current_site_id(self):
        if self.scope_var.get() == "all":
            return None
        if self.site_combo.current() < 0:
            return "UNSET"
        return self._site_keys[self.site_combo.current()]

    def _current_signature(self):
        site_component = "N/A" if self._is_standalone_plugin_selected() else self._current_site_id()
        return (site_component, self._selected_plugin_id(), tuple(sorted(self._current_params().items())))

    def _invalidate_preview(self):
        """Called on ANY change to scope, plugin selection, a param
        value, or the save checkbox - forces a fresh Preview before
        Commit can be used again, since a previous preview no longer
        reflects what's currently configured.
        """
        if not self._is_standalone_plugin_selected():
            self.site_combo.configure(state="readonly" if self.scope_var.get() == "site" else "disabled")
        self._last_previewed_signature = None
        self.commit_button.configure(state="disabled")
        self.preview_status_label.configure(text="Not yet previewed for the current settings.")

    def _refresh_site_choices(self):
        with db.get_conn() as conn:
            sites = db.get_all_sites(conn)
        self._site_keys = [site["id"] for site in sites]
        display_values = [f"{site['site_name'] or site['site_octet']} ({site['site_octet']})" for site in sites]
        self.site_combo["values"] = display_values
        if display_values:
            self.site_combo.current(0)

    def _append_console(self, text: str):
        self.console.configure(state="normal")
        self.console.insert("end", text)
        self.console.see("end")
        self.console.configure(state="disabled")

    def _clear_console(self):
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                item = self._output_queue.get_nowait()
                if item is _DONE:
                    self.preview_button.configure(state="normal")
                    completion = self._pending_completion
                    self._pending_completion = None
                    if completion and completion["success"] and not completion["commit"]:
                        self._last_previewed_signature = completion["signature"]
                        if completion["signature"] == self._current_signature():
                            self.commit_button.configure(state="normal")
                            self.preview_status_label.configure(text="Previewed - ready to commit.")
                    if completion and completion["commit"]:
                        # A commit always consumes the preview it was
                        # checked against - force a fresh one before
                        # any further commit, success or not.
                        self._last_previewed_signature = None
                        self.commit_button.configure(state="disabled")
                        self.preview_status_label.configure(text="Not yet previewed for the current settings.")
                else:
                    self._append_console(item)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _ensure_tacacs_available(self) -> bool:
        """The credential store is guaranteed unlocked (login succeeded
        before this tab was ever built) - this just confirms the
        'tacacs' type specifically has been added to it.
        """
        try:
            orchestrator._get_tacacs_credentials(self._creds)
        except ValueError as e:
            messagebox.showerror("Credentials", str(e))
            return False
        return True

    def _validate_before_run(self):
        if not self._is_standalone_plugin_selected() and self.scope_var.get() == "site" and self.site_combo.current() < 0:
            messagebox.showwarning("Command Runner", "Choose a site first, or select 'All sites'.")
            return False
        if self._selected_plugin_id() is None:
            messagebox.showwarning("Command Runner", "Choose a script to run.")
            return False
        missing = self._missing_required_params()
        if missing:
            labels = [self._plugins[self._selected_plugin_id()].PARAMS[i]["label"]
                      for i, p in enumerate(self._plugins[self._selected_plugin_id()].PARAMS) if p["name"] in missing]
            messagebox.showwarning("Command Runner", f"Fill in required field(s): {', '.join(labels)}")
            return False
        return True

    def _on_preview_clicked(self):
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        if not self._validate_before_run():
            return
        if not self._ensure_tacacs_available():
            return

        self._start_run(commit=False, save=False, signature=self._current_signature())

    def _on_commit_clicked(self):
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        if not self._validate_before_run():
            return
        if self._current_signature() != self._last_previewed_signature:
            messagebox.showwarning(
                "Command Runner",
                "Settings have changed since the last preview - run Preview again before committing.",
            )
            return
        if not messagebox.askyesno(
            "Confirm commit",
            "This will push real configuration changes to the devices shown in the last preview. Continue?",
        ):
            return
        if not self._ensure_tacacs_available():
            return

        self._start_run(commit=True, save=self.save_var.get(), signature=None)

    def _start_run(self, commit, save, signature):
        plugin_id = self._selected_plugin_id()
        params = self._current_params()
        site_id = self._current_site_id()
        creds = self._creds

        self._clear_console()
        self.preview_button.configure(state="disabled")
        self.commit_button.configure(state="disabled")

        self._worker_thread = threading.Thread(
            target=self._run_worker,
            args=(plugin_id, params, site_id, creds, commit, save, signature),
            daemon=True,
        )
        self._worker_thread.start()

    def _run_worker(self, plugin_id, params, site_id, creds, commit, save, signature):
        original_stdout = sys.stdout
        sys.stdout = _QueueWriter(self._output_queue)
        success = False
        try:
            username, password = orchestrator._get_tacacs_credentials(creds)
            secret = orchestrator._get_tacacs_secret(creds)
            module = self._plugins[plugin_id]
            if orchestrator.is_standalone_plugin(module):
                orchestrator.run_standalone_plugin(
                    module, params, username, password, secret=secret, commit=commit,
                )
            else:
                with db.get_conn() as conn:
                    orchestrator.run_plugin(
                        conn, module, params, site_id, username, password,
                        secret=secret, commit=commit, save=save,
                    )
            success = True
        except Exception as e:
            print(f"ERROR: {e}")
        finally:
            sys.stdout = original_stdout
            self._pending_completion = {"commit": commit, "signature": signature, "success": success}
            self._output_queue.put(_DONE)


class AddCredentialTypeDialog(tk.Toplevel):
    """Add a new credential type, or add new fields to an existing one
    - if the entered type name already exists in the store, the
    entered fields are merged into it rather than rejected, covering
    both "create a type" and "add a field to an existing type" through
    one form. Any number of arbitrarily-named fields, each
    independently marked sensitive or not - mirrors
    credential_manager.py's own _prompt_custom_fields().
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Add Credential Type / Fields")
        self.result = None
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._field_rows = []

        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text="Type name (e.g. tacacs, cucm, vtc):").grid(
            row=0, column=0, columnspan=3, sticky="w")
        self.type_name_entry = ttk.Entry(container, width=30)
        self.type_name_entry.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self.type_name_entry.focus_set()

        ttk.Label(container, text="Field name").grid(row=2, column=0, sticky="w")
        ttk.Label(container, text="Value").grid(row=2, column=1, sticky="w")
        ttk.Label(container, text="Sensitive").grid(row=2, column=2, sticky="w")

        self.rows_frame = ttk.Frame(container)
        self.rows_frame.grid(row=3, column=0, columnspan=3, sticky="w")

        self._add_field_row()

        ttk.Button(container, text="+ Add another field", command=self._add_field_row).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 10))

        self.error_label = ttk.Label(container, text="", foreground="#990000")
        self.error_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))

        button_row = ttk.Frame(container)
        button_row.grid(row=6, column=0, columnspan=3, sticky="e")
        ttk.Button(button_row, text="Cancel", command=self._on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(button_row, text="Save", command=self._on_save).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.wait_window(self)

    def _add_field_row(self):
        row_index = len(self._field_rows)
        name_entry = ttk.Entry(self.rows_frame, width=18)
        name_entry.grid(row=row_index, column=0, padx=(0, 6), pady=2)
        value_entry = ttk.Entry(self.rows_frame, width=18, show="*")
        value_entry.grid(row=row_index, column=1, padx=(0, 6), pady=2)
        sensitive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self.rows_frame, variable=sensitive_var,
            command=lambda: value_entry.configure(show="*" if sensitive_var.get() else ""),
        ).grid(row=row_index, column=2, pady=2)
        self._field_rows.append((name_entry, value_entry, sensitive_var))

    def _on_save(self):
        type_name = self.type_name_entry.get().strip()
        if not type_name:
            self.error_label.configure(text="Type name cannot be blank.")
            return

        fields = {}
        for name_entry, value_entry, sensitive_var in self._field_rows:
            field_name = name_entry.get().strip()
            if not field_name:
                continue
            fields[field_name] = {"value": value_entry.get(), "sensitive": sensitive_var.get()}

        if not fields:
            self.error_label.configure(text="Enter at least one field (with a name).")
            return

        self.result = (type_name, fields)
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()


class UpdateFieldDialog(tk.Toplevel):
    """Change a single existing field's value. Sensitivity stays as it
    already was on that field - use Add Type/Field to control how a
    NEW field is marked; this only ever updates the value.
    """

    def __init__(self, parent, type_name, field_name, sensitive):
        super().__init__(parent)
        self.title(f"Update {type_name}.{field_name}")
        self.result = None
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text=f"New value for '{type_name}.{field_name}':").pack(anchor="w", pady=(0, 6))
        self.value_entry = ttk.Entry(container, width=36, show="*" if sensitive else "")
        self.value_entry.pack(pady=(0, 10))
        self.value_entry.focus_set()
        self.value_entry.bind("<Return>", lambda e: self._on_save())

        button_row = ttk.Frame(container)
        button_row.pack(fill="x")
        ttk.Button(button_row, text="Cancel", command=self._on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(button_row, text="Save", command=self._on_save).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.wait_window(self)

    def _on_save(self):
        self.result = self.value_entry.get()
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()


class CredentialsTab(ttk.Frame):
    """View/add/update/delete entries in the credential store this
    session already unlocked at login - no separate unlock, no
    switching to a different store. Values are masked by default;
    revealing one is its own explicit, logged action, never a side
    effect of just browsing the list - mirrors credential_manager.py's
    own view/reveal split. Every add/update/delete/reveal is logged
    via db.log_activity() - the action and field NAME only, never the
    value itself.
    """

    def __init__(self, parent, path, creds, master_password):
        super().__init__(parent)
        self._path = path
        self._creds = creds
        self._master_password = master_password
        self._build_layout()
        self._refresh_tree()

    def _build_layout(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=10)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text=f"Store: {self._path}").pack(side="left")

        tree_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(tree_frame, columns=("value",), show="tree headings")
        self.tree.heading("#0", text="Type / Field")
        self.tree.heading("value", text="Value")
        self.tree.column("value", width=300)
        self.tree.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        button_row = ttk.Frame(self, padding=(10, 0, 10, 10))
        button_row.grid(row=2, column=0, sticky="ew")
        ttk.Button(button_row, text="Add Type / Field", command=self._on_add).pack(side="left")
        self.update_button = ttk.Button(button_row, text="Update Field", command=self._on_update, state="disabled")
        self.update_button.pack(side="left", padx=(6, 0))
        self.reveal_button = ttk.Button(button_row, text="Reveal Field", command=self._on_reveal, state="disabled")
        self.reveal_button.pack(side="left", padx=(6, 0))
        self.delete_button = ttk.Button(button_row, text="Delete Type", command=self._on_delete, state="disabled")
        self.delete_button.pack(side="left", padx=(6, 0))

        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_selection_changed())

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for type_name in sorted(self._creds.keys()):
            self.tree.insert("", "end", iid=type_name, text=type_name, values=("",), open=True)
            fields = self._creds[type_name]
            for field_name in sorted(fields.keys()):
                field = fields[field_name]
                shown = "********" if field["sensitive"] else field["value"]
                self.tree.insert(type_name, "end", iid=f"{type_name}::{field_name}", text=field_name, values=(shown,))
        self._on_selection_changed()

    def _selected_type_and_field(self):
        """Returns (type_name, field_name_or_None) for the current
        selection, or (None, None) if nothing is selected.
        """
        selection = self.tree.selection()
        if not selection:
            return None, None
        item_id = selection[0]
        if "::" in item_id:
            type_name, field_name = item_id.split("::", 1)
            return type_name, field_name
        return item_id, None

    def _on_selection_changed(self):
        type_name, field_name = self._selected_type_and_field()
        self.update_button.configure(state="normal" if field_name else "disabled")
        self.reveal_button.configure(state="normal" if field_name else "disabled")
        self.delete_button.configure(state="normal" if type_name and not field_name else "disabled")

    def _save_and_refresh(self):
        credential_store.save_and_encrypt(self._path, self._creds, self._master_password)
        self._refresh_tree()

    def _on_add(self):
        dialog = AddCredentialTypeDialog(self)
        if dialog.result is None:
            return
        type_name, fields = dialog.result

        is_new_type = type_name not in self._creds
        if is_new_type:
            self._creds[type_name] = {}
        self._creds[type_name].update(fields)

        try:
            self._save_and_refresh()
        except Exception as e:
            messagebox.showerror("Credentials", f"Could not save: {e}")
            return

        action = "add_credential_type" if is_new_type else "update_credential_field"
        db.log_activity(action, f"{'Added' if is_new_type else 'Updated'} {len(fields)} field(s) on '{type_name}'")

    def _on_update(self):
        type_name, field_name = self._selected_type_and_field()
        if not field_name:
            return
        current_sensitive = self._creds[type_name][field_name]["sensitive"]

        dialog = UpdateFieldDialog(self, type_name, field_name, current_sensitive)
        if dialog.result is None:
            return

        self._creds[type_name][field_name] = {"value": dialog.result, "sensitive": current_sensitive}
        try:
            self._save_and_refresh()
        except Exception as e:
            messagebox.showerror("Credentials", f"Could not save: {e}")
            return

        db.log_activity("update_credential_field", f"Updated '{type_name}.{field_name}'")

    def _on_reveal(self):
        type_name, field_name = self._selected_type_and_field()
        if not field_name:
            return
        if not messagebox.askyesno(
            "Reveal value",
            f"Reveal the real value of '{type_name}.{field_name}'? This will display it on screen.",
        ):
            return

        value = self._creds[type_name][field_name]["value"]
        messagebox.showinfo(f"{type_name}.{field_name}", value)
        db.log_activity("reveal_credential_field", f"Revealed '{type_name}.{field_name}'")

    def _on_delete(self):
        type_name, field_name = self._selected_type_and_field()
        if not type_name or field_name:
            return
        if not messagebox.askyesno(
            "Delete credential type",
            f"Delete the ENTIRE '{type_name}' credential (all its fields)? This cannot be undone.",
        ):
            return

        del self._creds[type_name]
        try:
            self._save_and_refresh()
        except Exception as e:
            messagebox.showerror("Credentials", f"Could not save: {e}")
            return

        db.log_activity("delete_credential_type", f"Deleted '{type_name}'")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Site Awareness Dashboard")
        self.geometry("900x560")

        db.init_db()

        login = LoginDialog(self)
        if login.result is None:
            # LoginDialog's own _on_exit() already calls sys.exit() on
            # every path out except a genuine success - reaching here
            # at all would mean something unexpected slipped through,
            # so exit defensively rather than ever showing a tab
            # without a logged-in identity.
            sys.exit(0)
        login_path, self.creds, master_password = login.result

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        self.site_manager_tab = SiteManagerTab(notebook)
        notebook.add(self.site_manager_tab, text="Site manager")
        notebook.add(DiscoveryTab(notebook, self.creds), text="Discovery")
        notebook.add(ExportTab(notebook), text="Export")
        notebook.add(CommandRunnerTab(notebook, self.creds), text="Command Runner")
        notebook.add(CredentialsTab(notebook, login_path, self.creds, master_password), text="Credentials")

        # Keeps the shared write queue moving even when this GUI is
        # just sitting open with nobody clicking anything - without
        # this, an item someone else queued (or one of this app's own
        # queued-but-not-yet-applied items, from a moment when the
        # queue was contended) would only ever get picked up the next
        # time THIS GUI happens to perform its own write. See
        # write_queue.py's module docstring for the full design.
        self._drain_in_progress = False
        self._schedule_periodic_drain()

    # --- background write-queue draining ---

    def _schedule_periodic_drain(self):
        self.after(DRAIN_INTERVAL_MS, self._periodic_drain)

    def _periodic_drain(self):
        # Guards against overlapping drains within THIS process only
        # (the drain lock file already prevents two different
        # processes from draining at once - this is purely "don't
        # start a second background thread here if the last one is
        # somehow still running", which try_drain() being slow or the
        # timer firing unusually fast could otherwise cause).
        if not self._drain_in_progress:
            self._drain_in_progress = True
            threading.Thread(target=self._periodic_drain_worker, daemon=True).start()
        self._schedule_periodic_drain()

    def _periodic_drain_worker(self):
        try:
            outcomes = write_queue.try_drain()
        except Exception as e:
            print(f"Warning: periodic write-queue drain failed: {e}")
            outcomes = {}
        finally:
            self._drain_in_progress = False
        if outcomes:
            # Hop back onto the main thread before touching any
            # Tkinter widget - self.after(0, ...) from a background
            # thread is the standard safe way to do that.
            self.after(0, self._on_periodic_drain_applied, outcomes)

    def _on_periodic_drain_applied(self, outcomes):
        # Something in the shared queue just got applied - possibly
        # queued by a completely different person's GUI - so refresh
        # the Site Manager tab to show it without anyone having to
        # click anything. refresh_sites() also re-selects the
        # previously-selected site (if it's still there) and cascades
        # into refresh_devices(), so this doesn't disrupt whatever the
        # person was looking at.
        #
        # site_manager_tab is guarded with getattr rather than assumed
        # to exist: this fires from a self.after(0, ...) callback
        # queued from a background thread, which Tk delivers on its
        # own schedule - if that lands at a moment this instance hasn't
        # (or no longer) has a live site_manager_tab (mid-teardown, or
        # a test harness driving these methods directly without
        # building a full App), skip the refresh rather than crash the
        # Tkinter callback loop over what is, either way, purely a
        # convenience refresh - the underlying write already succeeded
        # regardless of whether this step runs.
        tab = getattr(self, "site_manager_tab", None)
        if tab is not None and any(o["ok"] for o in outcomes.values()):
            tab.refresh_sites()


if __name__ == "__main__":
    app = App()
    app.mainloop()
