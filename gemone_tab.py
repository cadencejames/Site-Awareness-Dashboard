"""
gemone_tab.py - the "GEM One" tab for gui.py.

Two sub-tabs:

  Pull & import   runs the user's gemone_full_pull.py as a separate program,
                  hands it the GEM One login on standard input (one JSON
                  line, never written to disk, a command line or the log),
                  shows its output live (colored by its [INFO] / [WARN] /
                  [ERROR] tags, with a progress bar fed by [PROGRESS] lines),
                  then checks the file it wrote and imports it. Also:
                  "Import file only" and "Check file" for a file that is
                  already on disk.
  KG devices      every KG with its health, and a way to assign KGs to
                  sites (by hand, or from the serial-to-site CSV).

All checking and importing is done by gemone_import.py (run_import /
run_map), the same code the command line uses, so there is one
implementation, not two. Settings (script path, output file, timeout,
credential name) come from the "gemone" section of settings.json.

The credential entry (default name "gemone") needs two fields, "username"
and "password".
"""
import contextlib
import datetime
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "utilities"))

import db  # noqa: E402
import kg_health  # noqa: E402
import settings_loader  # noqa: E402
import write_queue  # noqa: E402
import gemone_import  # noqa: E402

_TAG = re.compile(r"^\[(INFO|WARN|ERROR|PROGRESS|SAD)\]\s?(.*)$")
_PROGRESS = re.compile(r"^(\d+)\s*/\s*(\d+)\s*(.*)$")
_DONE = object()

LOG_COLORS = {
    "INFO": "#dddddd", "WARN": "#ffb74d", "ERROR": "#ff6b6b",
    "SAD": "#4fc3f7", "PROGRESS": "#8a8a8a", "TIME": "#6e6e6e",
}
HEALTH_COLORS = {"critical": "#c62828", "warning": "#b26a00", "ok": "#1b5e20", "unknown": "#616161"}
_HEALTH_RANK = {"critical": 3, "warning": 2, "unknown": 1, "ok": 0}


class _LineEmitter:
    """A file-like object that hands each complete line to a callback; used
    to capture print() output from gemone_import and the dashboard generator."""

    def __init__(self, callback):
        self._callback = callback
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._callback(line)
        return len(text)

    def flush(self):
        pass

    def close(self):
        if self._buf.strip():
            self._callback(self._buf)
        self._buf = ""


def _classify_captured(line: str) -> str:
    """Colors a plain-text line from gemone_import: ERROR / warning lines get
    their own color, everything else is a SAD message."""
    low = line.strip().lower()
    if low.startswith("error"):
        return f"[ERROR] {line.split(':', 1)[1].strip() if ':' in line else line}"
    if "problem(s)" in low:
        return f"[ERROR] {line}"
    if "warning" in low:
        return f"[WARN] {line}"
    return f"[SAD] {line}"


class _SiteChoiceDialog(tk.Toplevel):
    """Modal: pick one site. self.result is the chosen site row or None."""

    def __init__(self, parent, sites, prompt):
        super().__init__(parent)
        self.title("Assign to site")
        self.result = None
        self._sites = sites
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        ttk.Label(self, text=prompt, padding=(12, 12, 12, 4)).pack(anchor="w")
        self.combo = ttk.Combobox(
            self, state="readonly", width=48,
            values=[f"{s['site_octet']}  -  {s['site_name'] or '(unset)'}" for s in sites])
        self.combo.pack(padx=12, pady=(0, 8))
        if sites:
            self.combo.current(0)
        row = ttk.Frame(self, padding=(12, 0, 12, 12))
        row.pack(fill="x")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Assign", command=self._ok).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    def _ok(self):
        i = self.combo.current()
        if i >= 0:
            self.result = self._sites[i]
        self.destroy()


class GemOneTab(ttk.Frame):
    def __init__(self, parent, creds):
        super().__init__(parent)
        self._creds = creds              # the unlocked store, shared with the Credentials tab
        self._queue = queue.Queue()
        self._thread = None
        self._proc = None
        self._cancelled = False
        self._timed_out = False
        self._warn_count = 0
        self._error_count = 0
        self._kg_rows = {}
        self._build_layout()
        self._refresh_info()
        self.refresh_kgs()
        self._poll()

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------

    def _build_layout(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.inner = ttk.Notebook(self)
        self.inner.grid(row=0, column=0, sticky="nsew")
        run_page = ttk.Frame(self.inner)
        kg_page = ttk.Frame(self.inner)
        self.inner.add(run_page, text="Pull & import")
        self.inner.add(kg_page, text="KG devices")
        self._build_run_page(run_page)
        self._build_kg_page(kg_page)

    def _build_run_page(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)

        cfg = self._cfg()
        top = ttk.Frame(page, padding=(10, 10, 10, 0))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="File:").grid(row=0, column=0, sticky="w")
        self.file_var = tk.StringVar(value=settings_loader.resolve_path(cfg["output_file"]))
        ttk.Entry(top, textvariable=self.file_var).grid(row=0, column=1, sticky="ew", padx=6)
        self.browse_button = ttk.Button(top, text="Browse...", command=self._on_browse)
        self.browse_button.grid(row=0, column=2)
        self.info_label = ttk.Label(top, text="", foreground="#555555")
        self.info_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        opts = ttk.Frame(page, padding=(10, 8, 10, 0))
        opts.grid(row=1, column=0, sticky="ew")
        self.dry_var = tk.BooleanVar(value=False)
        self.rebuild_var = tk.BooleanVar(value=True)
        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Dry run (check and show what would change, save nothing)",
                        variable=self.dry_var).pack(side="left")
        ttk.Checkbutton(opts, text="Rebuild dashboard after import",
                        variable=self.rebuild_var).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(opts, text="Allow older file",
                        variable=self.force_var).pack(side="left", padx=(14, 0))

        buttons = ttk.Frame(page, padding=(10, 8, 10, 0))
        buttons.grid(row=2, column=0, sticky="ew")
        self.pull_button = ttk.Button(buttons, text="Pull from GEM One and import", command=self._on_pull)
        self.pull_button.pack(side="left")
        self.import_button = ttk.Button(buttons, text="Import file only", command=self._on_import_only)
        self.import_button.pack(side="left", padx=(6, 0))
        self.check_button = ttk.Button(buttons, text="Check file", command=self._on_check)
        self.check_button.pack(side="left", padx=(6, 0))
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Clear log", command=self._clear_log).pack(side="right")

        prog = ttk.Frame(page, padding=(10, 8, 10, 0))
        prog.grid(row=3, column=0, sticky="ew")
        prog.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress_label = ttk.Label(prog, text="Idle", width=34, anchor="w")
        self.progress_label.grid(row=0, column=1, padx=(8, 0))

        log_frame = ttk.Frame(page, padding=(10, 8, 10, 10))
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(
            log_frame, height=14, state="disabled", wrap="word",
            background="#111111", foreground="#dddddd", insertbackground="#dddddd", font=("Consolas", 10))
        self.log.grid(row=0, column=0, sticky="nsew")
        for name, color in LOG_COLORS.items():
            self.log.tag_configure(name, foreground=color)
        self.log.tag_configure("ERROR", foreground=LOG_COLORS["ERROR"], font=("Consolas", 10, "bold"))

    def _build_kg_page(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        bar = ttk.Frame(page, padding=10)
        bar.grid(row=0, column=0, sticky="ew")
        self.assign_button = ttk.Button(bar, text="Assign selected to site...", command=self._on_assign, state="disabled")
        self.assign_button.pack(side="left")
        self.remove_button = ttk.Button(bar, text="Remove selected...", command=self._on_remove, state="disabled")
        self.remove_button.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Load site map CSV...", command=self._on_load_map).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Refresh", command=self.refresh_kgs).pack(side="left", padx=(6, 0))
        self.kg_count_label = ttk.Label(bar, text="")
        self.kg_count_label.pack(side="right")

        frame = ttk.Frame(page, padding=(10, 0, 10, 10))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        cols = ("name", "serial", "site", "state", "keymat", "health", "note")
        self.kg_tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for col, text, width in (("name", "Name", 170), ("serial", "Serial", 130), ("site", "Site", 150),
                                 ("state", "State", 110), ("keymat", "KEYMAT", 110), ("health", "Health", 80),
                                 ("note", "Note", 150)):
            self.kg_tree.heading(col, text=text)
            self.kg_tree.column(col, width=width, anchor="w")
        self.kg_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.kg_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.kg_tree.configure(yscrollcommand=scroll.set)
        for name, color in HEALTH_COLORS.items():
            self.kg_tree.tag_configure(name, foreground=color)
        self.kg_tree.bind("<<TreeviewSelect>>",
                          self._on_kg_selection)

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------

    def _cfg(self, quiet=False):
        try:
            return settings_loader.get_section("gemone")
        except settings_loader.SettingsError as e:
            if quiet:
                return dict(settings_loader.DEFAULTS["gemone"])
            messagebox.showerror("GEM One", f"{e}\n\nBuilt-in defaults will be used until settings.json is fixed.")
            return dict(settings_loader.DEFAULTS["gemone"])

    def _is_busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _refresh_info(self):
        try:
            with db.get_conn() as conn:
                last = db.get_kg_last_import(conn)
        except Exception:
            last = None
        if last is None:
            text = "No GEM One file has been imported yet."
        else:
            text = f"Last import: {last['imported_at']} (file collected {last['file_collected_at']})"
        path = self.file_var.get()
        if os.path.exists(path):
            when = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
            text += f"   |   File on disk saved {when}"
        else:
            text += "   |   File not found yet"
        self.info_label.configure(text=text)

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for b in (self.pull_button, self.import_button, self.check_button, self.browse_button):
            b.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if busy:
            self.assign_button.configure(state="disabled")
            self.remove_button.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _on_browse(self):
        path = filedialog.askopenfilename(
            title="Choose a GEM One JSON file", initialdir=os.path.dirname(self.file_var.get()) or HERE,
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if path:
            self.file_var.set(os.path.normpath(path))
            self._refresh_info()

    # ------------------------------------------------------------------
    # log display (main thread only)
    # ------------------------------------------------------------------

    def _emit(self, line: str):
        """Safe from any thread."""
        self._queue.put(line)

    def _append(self, raw: str):
        m = _TAG.match(raw)
        tag, text = (m.group(1), m.group(2)) if m else ("INFO", raw)
        if tag == "WARN":
            self._warn_count += 1
        elif tag == "ERROR":
            self._error_count += 1
        elif tag == "PROGRESS":
            pm = _PROGRESS.match(text)
            if pm:
                done, total = int(pm.group(1)), int(pm.group(2))
                if total > 0:
                    self.progress.configure(value=min(100.0, done * 100.0 / total))
                self.progress_label.configure(text=f"{done}/{total} {pm.group(3)}"[:40])
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", stamp + " ", "TIME")
        self.log.insert("end", f"[{tag}] {text}\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                item = self._queue.get_nowait()
                if item is _DONE:
                    self._on_finished()
                else:
                    self._append(item)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_finished(self):
        self._set_busy(False)
        self._proc = None
        if self._error_count:
            summary = f"Finished with {self._error_count} error(s) and {self._warn_count} warning(s)"
        elif self._warn_count:
            summary = f"Finished with {self._warn_count} warning(s)"
        else:
            summary = "Finished"
        self.progress_label.configure(text=summary[:40])
        self._refresh_info()
        self.refresh_kgs()

    # ------------------------------------------------------------------
    # starting jobs
    # ------------------------------------------------------------------

    def _begin(self, target, args=()):
        if self._is_busy():
            return
        self._warn_count = self._error_count = 0
        self._cancelled = self._timed_out = False
        self.progress.configure(value=0)
        self.progress_label.configure(text="Working...")
        self._set_busy(True)
        self._thread = threading.Thread(target=self._run_job, args=(target,) + tuple(args), daemon=True)
        self._thread.start()

    def _run_job(self, target, *args):
        try:
            target(*args)
        except Exception as e:      # never leave the tab stuck in "busy"
            self._emit(f"[ERROR] Unexpected problem: {type(e).__name__}: {str(e)[:150]}")
        finally:
            self._queue.put(_DONE)

    def _get_credentials(self):
        """(username, password) from the store, or raises ValueError with a plain reason."""
        name = self._cfg()["credential_name"]
        entry = next((v for k, v in self._creds.items() if k.lower() == name.lower()), None)
        if not entry:
            raise ValueError(f'The credential store has no "{name}" entry. Add one on the Credentials tab '
                             f'with the fields "username" and "password".')
        fields = {k.lower(): v for k, v in entry.items()}
        values = []
        for key in ("username", "password"):
            field = fields.get(key)
            value = field.get("value") if isinstance(field, dict) else field
            if not value:
                raise ValueError(f'The "{name}" credential has no "{key}" field (or it is empty).')
            values.append(str(value))
        return values[0], values[1]

    def _on_pull(self):
        try:
            user, password = self._get_credentials()
        except ValueError as e:
            messagebox.showerror("GEM One", str(e))
            return
        path = self.file_var.get().strip()
        if not path:
            messagebox.showwarning("GEM One", "Choose where the output file should go first.")
            return
        if not self.dry_var.get() and not messagebox.askyesno(
                "GEM One", "Pull from GEM One and import into the database?\n\nTick 'Dry run' first to check without saving."):
            return
        self._begin(self._job_pull, (user, password, path, self.dry_var.get(),
                                     self.rebuild_var.get(), self.force_var.get()))

    def _on_import_only(self):
        path = self.file_var.get().strip()
        if not self.dry_var.get() and not messagebox.askyesno(
                "GEM One", f"Import this file into the database?\n\n{path}\n\nTick 'Dry run' first to check without saving."):
            return
        self._begin(self._job_import, (path, self.dry_var.get(), self.rebuild_var.get(), self.force_var.get()))

    def _on_check(self):
        self._begin(self._job_check, (self.file_var.get().strip(),))

    def _on_cancel(self):
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._cancelled = True
            self._emit("[SAD] Cancelling...")
            try:
                proc.terminate()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # jobs (run on the worker thread)
    # ------------------------------------------------------------------

    def _job_check(self, path):
        self._emit(f"[SAD] Checking {path}")
        self._run_captured(gemone_import.run_import, path, False, False, True)

    def _job_import(self, path, dry, rebuild, force):
        self._emit(f"[SAD] {'Dry run: checking' if dry else 'Importing'} {path}")
        rc = self._run_captured(gemone_import.run_import, path, not dry, force, False)
        if rc == 0 and not dry and rebuild:
            self._rebuild_dashboard()

    def _run_captured(self, func, *args):
        emitter = _LineEmitter(lambda line: self._emit(_classify_captured(line)))
        original = sys.stdout
        try:
            with contextlib.redirect_stdout(emitter):
                rc = func(*args)
        finally:
            emitter.close()
            sys.stdout = original
        return rc

    def _rebuild_dashboard(self):
        import dashboard_generate
        self._emit("[SAD] Rebuilding dashboard pages")
        emitter = _LineEmitter(lambda line: self._emit(f"[SAD] {line.strip()}") if line.strip() else None)
        original = sys.stdout
        try:
            with contextlib.redirect_stdout(emitter):
                dashboard_generate.generate_all()
            self._emit("[SAD] Dashboard rebuilt")
        except Exception as e:
            self._emit(f"[ERROR] Dashboard rebuild failed: {type(e).__name__}: {str(e)[:150]}")
        finally:
            emitter.close()
            sys.stdout = original

    def _job_pull(self, user, password, out_path, dry, rebuild, force):
        cfg = self._cfg(quiet=True)
        script = settings_loader.resolve_path(cfg["script"])
        if not os.path.exists(script):
            self._emit(f"[ERROR] Script not found: {script} (see settings.json, gemone.script)")
            return
        timeout = cfg.get("run_timeout_seconds") or 1800
        self._emit(f"[SAD] Starting {os.path.basename(script)}")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.Popen(
                [sys.executable, script, "--creds-stdin", "--out", out_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=os.path.dirname(script), env=env, creationflags=flags)
        except OSError as e:
            self._emit(f"[ERROR] Could not start the script: {type(e).__name__}: {str(e)[:150]}")
            return
        self._proc = proc

        def on_timeout():
            if proc.poll() is None:
                self._timed_out = True
                self._emit(f"[ERROR] The script ran longer than {timeout} seconds and was stopped")
                try:
                    proc.kill()
                except OSError:
                    pass

        timer = threading.Timer(timeout, on_timeout)
        timer.daemon = True
        timer.start()
        try:
            try:
                proc.stdin.write(json.dumps({"username": user, "password": password}) + "\n")
                proc.stdin.flush()
                proc.stdin.close()
            except OSError:
                pass   # the script exited before reading; its output below says why
            mask = password if len(password) >= 4 else None
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if mask and mask in line:
                    line = line.replace(mask, "****")
                if line.strip():
                    self._emit(line)
            rc = proc.wait()
        finally:
            timer.cancel()
            password = None

        if self._cancelled:
            self._emit("[SAD] Cancelled. Nothing was imported.")
            return
        if self._timed_out:
            self._emit("[SAD] Nothing was imported.")
            return
        if rc != 0:
            self._emit(f"[ERROR] The script exited with code {rc}. Nothing was imported.")
            return
        self._emit("[SAD] Script finished. Checking the file it wrote.")
        self._job_import(out_path, dry, rebuild, force)

    # ------------------------------------------------------------------
    # KG devices page
    # ------------------------------------------------------------------

    def refresh_kgs(self):
        cfg = self._cfg()
        self.kg_tree.delete(*self.kg_tree.get_children())
        self._kg_rows = {}
        try:
            with db.get_conn() as conn:
                rows = [dict(r) for r in db.get_kg_devices(conn)]
        except Exception as e:
            self.kg_count_label.configure(text=f"Could not read KGs: {type(e).__name__}")
            return
        today = datetime.date.today()
        for k in rows:
            k["ev"] = kg_health.evaluate(k, cfg, today, unreachable=k["assoc_unreachable"])
        rows.sort(key=lambda k: (-_HEALTH_RANK[k["ev"]["health"]],
                                 k["ev"]["keymat_days"] is None, k["ev"]["keymat_days"] or 0,
                                 (k["system_name"] or k["serial_number"]).lower()))
        unassigned = 0
        for k in rows:
            ev = k["ev"]
            site = k["site_octet"] or "unassigned"
            if site == "unassigned":
                unassigned += 1
                site_text = "Unassigned"
            else:
                site_text = f"{site}  -  {k['site_name']}" if k["site_name"] else site
            days = ev["keymat_days"]
            keymat = "-" if days is None else ("expired" if days < 0 else f"{days} days")
            note = "not in latest file" if k["missing_since"] else ("last pull failed" if k["last_error"] else "")
            self.kg_tree.insert("", "end", iid=k["serial_number"], tags=(ev["health"],), values=(
                k["system_name"] or "-", k["serial_number"], site_text, k["state"] or "unknown", keymat, ev["health"], note))
            self._kg_rows[k["serial_number"]] = k
        text = f"{len(rows)} KG(s)"
        if unassigned:
            text += f", {unassigned} with no site"
        self.kg_count_label.configure(text=text)

    def _on_kg_selection(self, event=None):
        state = "normal" if self.kg_tree.selection() and not self._is_busy() else "disabled"
        self.assign_button.configure(state=state)
        self.remove_button.configure(state=state)

    def _on_remove(self):
        serials = list(self.kg_tree.selection())
        if not serials or self._is_busy():
            return
        names = "\n".join(f"  {self._kg_rows[s]['system_name'] or '-'}  ({s})" for s in serials[:10])
        if len(serials) > 10:
            names += f"\n  ... and {len(serials) - 10} more"
        if not messagebox.askyesno(
                "Remove KGs",
                f"Remove {len(serials)} KG(s) from SAD?\n\n{names}\n\n"
                "They disappear from the dashboard. If a later GEM One file lists one again, it comes back by itself."):
            return
        failed = []
        for serial in serials:
            try:
                write_queue.queue_and_wait("soft_delete_kg_device", timeout=30, serial_number=serial)
            except (RuntimeError, TimeoutError) as e:
                failed.append(f"{serial}: {e}")
        self.refresh_kgs()
        if failed:
            messagebox.showerror("Remove KGs", "Some removals failed:\n\n" + "\n".join(failed[:10]))
        else:
            messagebox.showinfo("Remove KGs", "Removed. Regenerate the dashboard to update the pages.")

    def _on_assign(self):
        serials = list(self.kg_tree.selection())
        if not serials or self._is_busy():
            return
        try:
            with db.get_conn() as conn:
                sites = list(db.get_all_sites(conn))
        except Exception as e:
            messagebox.showerror("Assign to site", str(e))
            return
        sites = [s for s in sites if s["site_octet"] != "unassigned"] + [s for s in sites if s["site_octet"] == "unassigned"]
        names = ", ".join(self._kg_rows[s]["system_name"] or s for s in serials[:3]) + (" ..." if len(serials) > 3 else "")
        dialog = _SiteChoiceDialog(self, sites, f"Assign {len(serials)} KG(s) to which site?\n({names})")
        if dialog.result is None:
            return
        site = dialog.result
        failed = []
        for serial in serials:
            try:
                write_queue.queue_and_wait("set_kg_site", timeout=30, serial_number=serial, site_id=site["id"])
            except (RuntimeError, TimeoutError) as e:
                failed.append(f"{serial}: {e}")
        self.refresh_kgs()
        if failed:
            messagebox.showerror("Assign to site", "Some assignments failed:\n\n" + "\n".join(failed[:10]))
        else:
            messagebox.showinfo("Assign to site", "Saved. Regenerate the dashboard to see it on the site pages.")

    def _on_load_map(self):
        cfg = self._cfg()
        default = settings_loader.resolve_path(cfg["site_map_file"])
        path = filedialog.askopenfilename(
            title="Choose the serial-to-site CSV", initialdir=os.path.dirname(default), initialfile=os.path.basename(default),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        preview = io.StringIO()
        with contextlib.redirect_stdout(preview):
            rc = gemone_import.run_map(path, False)
        if rc != 0:
            messagebox.showerror("Site map", preview.getvalue().strip())
            return
        if not messagebox.askyesno("Site map", preview.getvalue().strip() + "\n\nSave these assignments?"):
            return
        result = io.StringIO()
        with contextlib.redirect_stdout(result):
            rc = gemone_import.run_map(path, True)
        (messagebox.showinfo if rc == 0 else messagebox.showerror)("Site map", result.getvalue().strip())
        self.refresh_kgs()