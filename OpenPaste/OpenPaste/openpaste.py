#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenPaste - a lightweight clipboard history manager.

Your copies are remembered automatically. Ctrl+C keeps working exactly as
always (OpenPaste only *watches* the clipboard, it never intercepts keys).
Press Ctrl+Alt+C to open the OpenPaste panel and pick any of your past
copies: it is put back on the clipboard and pasted into the app you were
using.

Usage:
    python openpaste.py            start OpenPaste in the background
    python openpaste.py toggle     open/close the panel of a running instance
    python openpaste.py show       open the panel of a running instance
    python openpaste.py quit       stop a running instance

Dependencies:
    pip install pyperclip pynput
    (Linux only: sudo apt install xclip   - or xsel / wl-clipboard)
"""

from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------------
# Configuration - edit these values to taste
# ----------------------------------------------------------------------------
HOTKEY = "<ctrl>+<alt>+c"     # pynput hotkey that toggles the panel
MAX_ITEMS = 50                # history size (pinned items never expire)
POLL_INTERVAL = 0.3           # seconds between clipboard checks
AUTO_PASTE = True             # picked items are pasted into the previous app
PASTE_DELAY_MS = 150          # wait for the previous window to regain focus
MAX_TEXT_CHARS = 100_000      # skip copies larger than this (characters)
PREVIEW_MAX = 90              # preview length of one row (characters)
IPC_PORT = 57310              # localhost port used for single-instance + IPC
BORDERLESS = True             # borderless popup look (set False if your
                              # window manager has trouble focusing it)

DATA_DIR = Path(os.environ.get("OPENPASTE_HOME", str(Path.home() / ".openpaste")))
HISTORY_FILE = DATA_DIR / "history.json"

# ----------------------------------------------------------------------------
# Dark theme
# ----------------------------------------------------------------------------
C_BG = "#14141c"        # panel background
C_SURFACE = "#1d1d2a"   # header / footer strips
C_ROW = "#20202e"       # normal row
C_ROW_SEL = "#2d2d44"   # selected row
C_ACCENT = "#7c5cff"    # purple accent
C_TEXT = "#e9e9f2"      # main text
C_MUTED = "#8b8ba3"     # secondary text
C_DANGER = "#ff6b81"    # delete button
C_PIN = "#f0b429"       # pin marker

# ============================================================================
# History storage (no GUI imports here, so this part is unit-testable)
# ============================================================================


class HistoryStore:
    """Holds clipboard entries, newest first, and persists them to JSON."""

    def __init__(self, path: Path = HISTORY_FILE, max_items: int = MAX_ITEMS):
        self.path = Path(path)
        self.max_items = max_items
        self.lock = threading.Lock()
        self.items: list[dict] = []  # {"text": str, "ts": float, "pinned": bool}
        self.load()

    # -- persistence ---------------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                clean = []
                for it in raw:
                    if isinstance(it, dict) and isinstance(it.get("text"), str):
                        clean.append({
                            "text": it["text"],
                            "ts": float(it.get("ts", time.time())),
                            "pinned": bool(it.get("pinned", False)),
                        })
                self.items = clean
        except FileNotFoundError:
            self.items = []
        except Exception as exc:  # corrupted file - start fresh
            print(f"[OpenPaste] could not read {self.path}: {exc}")
            self.items = []

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.items, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception as exc:
            print(f"[OpenPaste] could not save history: {exc}")

    # -- mutations -----------------------------------------------------------
    def add(self, text: str) -> bool:
        """Add a new copy. Returns True if the history changed."""
        if not text or not text.strip():
            return False
        if len(text) > MAX_TEXT_CHARS:
            return False
        with self.lock:
            for it in self.items:
                if it["text"] == text:
                    if it["pinned"]:
                        return False  # already kept forever, no change
                    # re-copy of an old item -> move it back to the top
                    self.items.remove(it)
                    self.items.insert(0, {"text": text, "ts": time.time(),
                                          "pinned": False})
                    return True
            self.items.insert(0, {"text": text, "ts": time.time(),
                                  "pinned": False})
            self._trim()
            return True

    def _trim(self) -> None:
        unpinned = [it for it in self.items if not it["pinned"]]
        excess = len(unpinned) - self.max_items
        if excess > 0:
            for it in unpinned[-excess:]:  # oldest unpinned entries
                self.items.remove(it)

    def touch(self, item: dict) -> None:
        """Picked an item: bump unpinned entries to the top and save."""
        with self.lock:
            if not item["pinned"]:
                self.items.remove(item)
                item["ts"] = time.time()
                self.items.insert(0, item)
            self.save()

    def toggle_pin(self, item: dict) -> None:
        with self.lock:
            item["pinned"] = not item["pinned"]
            if item["pinned"]:
                item["ts"] = time.time()
            self._trim()
            self.save()

    def delete(self, item: dict) -> None:
        with self.lock:
            if item in self.items:
                self.items.remove(item)
            self.save()

    def clear_unpinned(self) -> int:
        with self.lock:
            keep = [it for it in self.items if it["pinned"]]
            removed = len(self.items) - len(keep)
            self.items = keep
            self.save()
            return removed


# ============================================================================
# Helpers shared by monitor / GUI / CLI
# ============================================================================


def time_ago(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return datetime.fromtimestamp(ts).strftime("%d %b %Y")


def preview_of(text: str) -> str:
    first = text.splitlines()[0] if text.splitlines() else ""
    first = first.strip()
    extra = len(text.splitlines()) - 1
    if len(first) > PREVIEW_MAX:
        first = first[:PREVIEW_MAX].rstrip() + "..."
    if extra > 0:
        first += f"  (+{extra} more line{'s' if extra > 1 else ''})"
    return first


def simulate_paste() -> None:
    """Send Ctrl+V (Cmd+V on macOS) to whatever window now has focus."""
    try:
        from pynput.keyboard import Controller, Key
    except Exception as exc:
        print(f"[OpenPaste] cannot auto-paste (pynput missing): {exc}")
        return
    try:
        kb = Controller()
        mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
        kb.press(mod)
        time.sleep(0.03)
        kb.press("v")
        time.sleep(0.03)
        kb.release("v")
        kb.release(mod)
    except Exception as exc:
        print(f"[OpenPaste] auto-paste failed: {exc}")


class ClipboardMonitor(threading.Thread):
    """Watches the system clipboard and records every new copy."""

    def __init__(self, store: HistoryStore, ui_queue: "queue.Queue[str]"):
        super().__init__(daemon=True, name="clipboard-monitor")
        self.store = store
        self.ui_queue = ui_queue
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import pyperclip  # imported here so --help / logic tests need no GUI OS
        last = None
        while not self._stop.is_set():
            try:
                current = pyperclip.paste()
            except Exception:
                current = None  # clipboard busy or backend hiccup
            if isinstance(current, str) and current and current != last:
                last = current
                if self.store.add(current):
                    self.store.save()
                    self.ui_queue.put("refresh")
            time.sleep(POLL_INTERVAL)


def start_global_hotkey(ui_queue: "queue.Queue[str]"):
    """Register the Ctrl+Alt+C global hotkey. Returns the listener or None."""
    try:
        from pynput import keyboard
    except Exception as exc:
        print(f"[OpenPaste] pynput unavailable, hotkey disabled: {exc}")
        return None
    try:
        listener = keyboard.GlobalHotKeys({HOTKEY: lambda: ui_queue.put("toggle")})
        listener.daemon = True
        listener.start()
        return listener
    except Exception as exc:
        print(f"[OpenPaste] global hotkey unavailable: {exc}")
        return None


def send_ipc(command: str) -> bool:
    """Send a command to a running OpenPaste instance."""
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=0.6) as s:
            s.sendall(command.encode())
        return True
    except OSError:
        return False


# ============================================================================
# GUI
# ============================================================================

ROW_H = 46          # px, used for scroll-into-view maths
PANEL_W = 470
LIST_H = 430


class OpenPasteApp:
    def __init__(self, store: HistoryStore, ui_queue: "queue.Queue[str]",
                 show_now: bool = False):
        import tkinter as tk
        from tkinter import font as tkfont  # noqa: F401  (forces font engine)
        self.tk = tk
        self.store = store
        self.ui_queue = ui_queue

        self.root = tk.Tk()
        self.root.title("OpenPaste")
        self.root.configure(bg=C_BG)
        try:
            base = tkfont.nametofont("TkDefaultFont")
            base.configure(size=10)
        except Exception:
            pass

        self.filtered: list[dict] = []
        self.sel_idx = 0
        self.pending_pick: str | None = None
        self._status_job = None

        self._build_panel()
        self.root.withdraw()  # start hidden, popup appears on hotkey
        self.root.protocol("WM_DELETE_WINDOW", self.hide_panel)

        self.refresh()
        if show_now:
            self.ui_queue.put("show")
        self.root.after(80, self._poll_queue)

    # -- panel construction ---------------------------------------------------
    def _build_panel(self) -> None:
        tk = self.tk
        try:
            self.root.overrideredirect(bool(BORDERLESS))
        except Exception:
            pass
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass

        # header ---------------------------------------------------------
        header = tk.Frame(self.root, bg=C_SURFACE)
        header.pack(fill="x")
        dot = tk.Label(header, text="OpenPaste", bg=C_SURFACE, fg=C_ACCENT,
                       font=("TkDefaultFont", 11, "bold"))
        dot.pack(side="left", padx=12, pady=8)
        self.count_lbl = tk.Label(header, text="", bg=C_SURFACE, fg=C_MUTED)
        self.count_lbl.pack(side="left", padx=4)
        close = tk.Label(header, text="x", bg=C_SURFACE, fg=C_MUTED,
                         cursor="hand2")
        close.pack(side="right", padx=12)
        close.bind("<Button-1>", lambda e: self.hide_panel())

        # search ----------------------------------------------------------
        search_wrap = tk.Frame(self.root, bg=C_BG)
        search_wrap.pack(fill="x", padx=10, pady=(8, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh())
        self.search = tk.Entry(search_wrap, textvariable=self.search_var,
                               bg=C_SURFACE, fg=C_TEXT, insertbackground=C_TEXT,
                               relief="flat", highlightthickness=1,
                               highlightbackground=C_ROW_SEL,
                               highlightcolor=C_ACCENT)
        self.search.pack(fill="x", ipady=6, padx=1)
        self._placeholder_on()
        self.search.bind("<FocusIn>", self._placeholder_off)
        self.search.bind("<FocusOut>", self._placeholder_on)

        # list area --------------------------------------------------------
        list_area = tk.Frame(self.root, bg=C_BG)
        list_area.pack(fill="both", expand=True, padx=10, pady=4)
        self.canvas = tk.Canvas(list_area, bg=C_BG, highlightthickness=0)
        vsb = tk.Scrollbar(list_area, orient="vertical",
                           command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.rows_frame = tk.Frame(self.canvas, bg=C_BG)
        self._rows_win = self.canvas.create_window((0, 0),
                                                   window=self.rows_frame,
                                                   anchor="nw")
        self.rows_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._rows_win, width=e.width))
        self.empty_lbl = None

        # footer ------------------------------------------------------------
        footer = tk.Frame(self.root, bg=C_SURFACE)
        footer.pack(fill="x", side="bottom")
        self.status = tk.Label(footer, text="", bg=C_SURFACE, fg=C_MUTED)
        self.status.pack(side="left", padx=10, pady=6)
        clear = tk.Label(footer, text="Clear all", bg=C_SURFACE, fg=C_MUTED,
                         cursor="hand2")
        clear.pack(side="right", padx=10)
        clear.bind("<Button-1>", lambda e: self.clear_all())
        quit_l = tk.Label(footer, text="Quit", bg=C_SURFACE, fg=C_DANGER,
                          cursor="hand2")
        quit_l.pack(side="right")
        quit_l.bind("<Button-1>", lambda e: self.quit_app())

        hint = tk.Label(self.root, bg=C_BG, fg=C_MUTED,
                        text="Enter paste  |  Shift+Enter copy only  |  "
                             "Del delete  |  Ctrl+P pin  |  Esc close")
        hint.pack(fill="x", side="bottom", pady=(0, 2))

        # keyboard ------------------------------------------------------------
        b = self.root.bind
        b("<Escape>", lambda e: self.hide_panel())
        b("<Return>", lambda e: self.pick_selected(and_paste=True))
        b("<Shift-Return>", lambda e: self.pick_selected(and_paste=False))
        b("<Up>", lambda e: self.move_sel(-1))
        b("<Down>", lambda e: self.move_sel(1))
        b("<Delete>", lambda e: self.delete_selected())
        b("<Control-p>", lambda e: self.pin_selected())

    # -- placeholder -------------------------------------------------------
    def _placeholder_on(self, _e=None) -> None:
        if not self.search_var.get():
            self.search_var.set("Search your copies...")
            self.search.config(fg=C_MUTED)

    def _placeholder_off(self, _e=None) -> None:
        if self.search.get() == "Search your copies..." and self.search.cget(
                "fg") == C_MUTED:
            self.search_var.set("")
            self.search.config(fg=C_TEXT)

    # -- queue / events ------------------------------------------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                ev = self.ui_queue.get_nowait()
                if ev == "toggle":
                    self.toggle_panel()
                elif ev == "show":
                    self.show_panel()
                elif ev == "refresh":
                    if self.root.winfo_viewable():
                        self.refresh()
                elif ev == "quit":
                    self.quit_app()
                    return
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    # -- panel visibility ------------------------------------------------------
    def toggle_panel(self) -> None:
        if self.root.winfo_viewable():
            self.hide_panel()
        else:
            self.show_panel()

    def show_panel(self) -> None:
        self.refresh()
        self.root.update_idletasks()
        w = PANEL_W
        h = min(int(self.root.winfo_reqheight()), int(self.root.winfo_screenheight() * 0.85))
        x = self.root.winfo_pointerx() - 40
        y = self.root.winfo_pointery() - 20
        x = max(0, min(x, self.root.winfo_screenwidth() - w))
        y = max(0, min(y, self.root.winfo_screenheight() - h))
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        self.search.focus_set()
        self.search.select_range(0, "end")
        self.search.icursor("end")

    def hide_panel(self) -> None:
        self.root.withdraw()

    # -- list rendering -----------------------------------------------------
    def refresh(self) -> None:
        q = self.search_var.get().strip().lower()
        if q in ("", "search your copies..."):
            self.filtered = list(self.store.items)
        else:
            self.filtered = [it for it in self.store.items
                             if q in it["text"].lower()]
        self.sel_idx = 0 if self.filtered else -1
        self._rebuild_rows()
        pinned = sum(1 for it in self.store.items if it["pinned"])
        total = len(self.store.items)
        self.count_lbl.config(text=f"{total} copies" + (f" | {pinned} pinned"
                                                        if pinned else ""))

    def _rebuild_rows(self) -> None:
        tk = self.tk
        for child in self.rows_frame.winfo_children():
            child.destroy()
        if self.empty_lbl:
            self.empty_lbl = None

        if not self.filtered:
            self.empty_lbl = tk.Label(
                self.rows_frame, bg=C_BG, fg=C_MUTED, justify="center",
                text=("No copies yet.\nCopy anything anywhere - it will\n"
                      "appear here automatically.")
                if not self.store.items else
                "Nothing matches your search.")
            self.empty_lbl.pack(pady=40, fill="x")
            return

        for i, item in enumerate(self.filtered):
            selected = (i == self.sel_idx)
            row = tk.Frame(self.rows_frame, bg=C_ROW_SEL if selected else C_ROW)
            row.pack(fill="x", pady=1)
            if selected:
                tk.Frame(row, bg=C_ACCENT, width=3).pack(side="left", fill="y")

            body = tk.Frame(row, bg=row.cget("bg"))
            body.pack(side="left", fill="x", expand=True, padx=(8, 4))
            prev = tk.Label(body, text=preview_of(item["text"]), anchor="w",
                            bg=row.cget("bg"), fg=C_TEXT)
            prev.pack(anchor="w", pady=(4, 0))
            meta_bits = [time_ago(item["ts"])]
            if item["pinned"]:
                meta_bits.append("PINNED")
            meta = tk.Label(body, text="   ".join(meta_bits), anchor="w",
                            bg=row.cget("bg"),
                            fg=C_PIN if item["pinned"] else C_MUTED)
            meta.pack(anchor="w", pady=(0, 4))

            act = tk.Frame(row, bg=row.cget("bg"))
            act.pack(side="right", padx=6)
            pin_b = tk.Label(act, text="PIN" if not item["pinned"] else "UNPIN",
                             bg=row.cget("bg"), fg=C_PIN, cursor="hand2")
            pin_b.pack(side="left", padx=3)
            del_b = tk.Label(act, text="DEL", bg=row.cget("bg"),
                             fg=C_DANGER, cursor="hand2")
            del_b.pack(side="left", padx=3)

            click = lambda e, it=item: self.pick(it, and_paste=True)
            copy_click = lambda e, it=item: self.pick(it, and_paste=False)
            for w in (row, body, prev, meta):
                w.bind("<Button-1>", click)
                w.bind("<Button-3>", lambda e, it=item: self._row_menu(it))
            pin_b.bind("<Button-1>", lambda e, it=item: self.toggle_pin(it))
            del_b.bind("<Button-1>", lambda e, it=item: self.delete_item(it))

        self._scroll_sel_into_view()

    def _row_menu(self, item: dict) -> None:
        from tkinter import Menu
        menu = Menu(self.root, tearoff=0,
                    bg=C_SURFACE, fg=C_TEXT, activebackground=C_ROW_SEL,
                    activeforeground=C_TEXT)
        menu.add_command(label="Copy only (no paste)",
                         command=lambda: self.pick(item, and_paste=False))
        menu.add_command(
            label="Unpin" if item["pinned"] else "Pin to top",
            command=lambda: self.toggle_pin(item))
        menu.add_command(label="Delete",
                         command=lambda: self.delete_item(item))
        try:
            menu.tk_popup(self.root.winfo_pointerx(),
                          self.root.winfo_pointery())
        finally:
            menu.grab_release()

    def _scroll_sel_into_view(self) -> None:
        if not self.filtered or self.sel_idx < 0:
            return
        total = max(1, len(self.filtered) * ROW_H)
        frac = (self.sel_idx * ROW_H) / total
        self.canvas.yview_moveto(max(0.0, frac - 0.12))

    # -- actions -----------------------------------------------------------
    def move_sel(self, delta: int) -> None:
        if not self.filtered:
            return
        self.sel_idx = max(0, min(len(self.filtered) - 1,
                                  self.sel_idx + delta))
        self._rebuild_rows()

    def pick_selected(self, and_paste: bool) -> None:
        if 0 <= self.sel_idx < len(self.filtered):
            self.pick(self.filtered[self.sel_idx], and_paste)

    def delete_selected(self) -> None:
        if 0 <= self.sel_idx < len(self.filtered):
            self.delete_item(self.filtered[self.sel_idx])

    def pin_selected(self) -> None:
        if 0 <= self.sel_idx < len(self.filtered):
            self.toggle_pin(self.filtered[self.sel_idx])

    def pick(self, item: dict, and_paste: bool) -> None:
        try:
            import pyperclip
            pyperclip.copy(item["text"])
        except Exception as exc:
            self.flash(f"Copy failed: {exc}")
            return
        self.store.touch(item)
        self.pending_pick = item["text"] if (and_paste and AUTO_PASTE) else None
        self.hide_panel()
        if self.pending_pick is not None:
            self.root.after(PASTE_DELAY_MS, self._do_paste)

    def _do_paste(self) -> None:
        if self.pending_pick is not None:
            self.pending_pick = None
            simulate_paste()

    def toggle_pin(self, item: dict) -> None:
        self.store.toggle_pin(item)
        self.refresh()
        self.flash("Unpinned" if not item["pinned"] else "Pinned to top")

    def delete_item(self, item: dict) -> None:
        self.store.delete(item)
        self.refresh()
        self.flash("Deleted")

    def clear_all(self) -> None:
        n = self.store.clear_unpinned()
        self.refresh()
        self.flash(f"Cleared {n} item{'s' if n != 1 else ''} (pinned kept)")

    def flash(self, msg: str) -> None:
        self.status.config(text=msg, fg=C_ACCENT)
        if self._status_job:
            self.root.after_cancel(self._status_job)
        self._status_job = self.root.after(
            2500, lambda: self.status.config(text="", fg=C_MUTED))

    # -- lifecycle -----------------------------------------------------------
    def quit_app(self) -> None:
        self.store.save()
        try:
            self.root.destroy()
        except Exception:
            pass


# ============================================================================
# main
# ============================================================================


def check_clipboard_backend() -> bool:
    """Make sure pyperclip can actually read the clipboard on this system."""
    try:
        import pyperclip  # noqa: F401
        pyperclip.paste()
        return True
    except Exception as exc:
        print("[OpenPaste] clipboard backend not ready:")
        print(f"  {exc}")
        print("  Fix:  pip install pyperclip")
        print("  Linux: sudo apt install xclip   (X11)")
        print("         sudo apt install wl-clipboard  (Wayland)")
        return False


def main() -> None:
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else ""

    # single instance: try to own the IPC port, otherwise talk to the owner
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", IPC_PORT))
        srv.listen(2)
        srv.settimeout(0.2)
    except OSError:
        target = cmd if cmd in ("toggle", "show", "quit") else "toggle"
        if send_ipc(target):
            print(f"[OpenPaste] already running - sent '{target}'.")
        else:
            print("[OpenPaste] another instance seems to be running "
                  f"(port {IPC_PORT} busy).")
        sys.exit(0)

    if cmd == "quit":  # fresh start with a quit command: nothing to do
        sys.exit(0)

    print(f"[OpenPaste] running - press {HOTKEY.replace('<', '').replace('>', '')} "
          f"to open your clipboard history.")
    print("[OpenPaste] launch again with 'toggle' / 'show' / 'quit' as needed.")

    if sys.platform.startswith("linux") and \
            os.environ.get("XDG_SESSION_TYPE") == "wayland":
        print("[OpenPaste] note: Wayland detected. Global hotkeys and "
              "auto-paste may need an X11 session; the clipboard history "
              "itself still works.")

    if not check_clipboard_backend():
        sys.exit(1)

    ui_queue: "queue.Queue[str]" = queue.Queue()
    store = HistoryStore()

    app = OpenPasteApp(store, ui_queue, show_now=(cmd in ("show", "toggle")))

    monitor = ClipboardMonitor(store, ui_queue)
    monitor.start()

    hotkey_listener = start_global_hotkey(ui_queue)

    def ipc_server() -> None:
        while True:
            try:
                conn, _ = srv.accept()
                data = conn.recv(64).decode(errors="ignore").strip()
                conn.close()
                if data in ("toggle", "show", "quit"):
                    ui_queue.put(data)
            except socket.timeout:
                continue
            except OSError:
                break

    threading.Thread(target=ipc_server, daemon=True,
                     name="ipc-server").start()

    try:
        app.root.mainloop()
    finally:
        monitor.stop()
        if hotkey_listener:
            try:
                hotkey_listener.stop()
            except Exception:
                pass
        store.save()
        srv.close()


if __name__ == "__main__":
    main()
