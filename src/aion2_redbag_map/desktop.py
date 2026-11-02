"""The single user-facing Windows management window; collection is opt-in."""
from __future__ import annotations

import queue
from pathlib import Path
import threading

from . import __version__
from .desktop_text import LANGUAGE_NAMES, error_text, text
from .localization import SUPPORTED_LANGUAGES, restored_language
from .runtime import RuntimeController

BG = "#101722"
CARD = "#182331"
BORDER = "#293747"
TEXT = "#edf2f7"
MUTED = "#91a2b5"
ACCENT = "#ee7064"
RECEIVING = "#68d6ae"
WARNING = "#e6ba79"


class DesktopWindow:
    def __init__(self, controller, *, start_receiving=False, show_overlay=False, minimized=False):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from .ui_theme import enable_dpi_awareness, ui_scale
        self.tk, self.ttk, self.filedialog, self.messagebox = tk, ttk, filedialog, messagebox
        self.controller = controller
        self.messages = queue.Queue()
        self.busy = self.polling = self.closing = False
        self.last_status = {}
        self.language = restored_language(controller.language)
        self.localized_widgets = []
        self.feedback_item = None
        self.pending_language = None
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.scale = ui_scale(self.root)
        self.px = lambda value: round(value * self.scale)
        self.root.title(self.t("title"))
        icon = Path(__file__).resolve().parent / "redbag.ico"
        if icon.is_file():
            try:
                self.root.iconbitmap(str(icon))
            except tk.TclError:
                pass
        self.root.geometry(f"{self.px(800)}x{self.px(640)}")
        self.root.minsize(self.px(760), self.px(640))
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_interface()
        self._set_buttons()
        self.root.after(100, self.drain)
        self.root.after(50, self.poll)
        self.schedule_startup(start_receiving=start_receiving, show_overlay=show_overlay, minimized=minimized)

    def t(self, key):
        return text(key, getattr(self, "language", "zh-CN"))

    def localized(self, widget, key):
        self.localized_widgets.append((widget, key))
        widget.configure(text=self.t(key))
        return widget

    def set_feedback(self, value, color=MUTED, *, error=False):
        self.feedback_item = (value, color, error)
        self.feedback.configure(text=error_text(value, self.language) if error else self.t(value), fg=color)

    def apply_language(self, language):
        self.language = restored_language(language)
        self.root.title(self.t("title"))
        dialog = getattr(self, "alert_dialog", None)
        if dialog is not None and dialog.root.winfo_exists():
            dialog.set_language(self.language)
        for widget, key in self.localized_widgets:
            widget.configure(text=self.t(key))
        self.language_select.current(SUPPORTED_LANGUAGES.index(self.language))
        if self.feedback_item:
            value, color, error = self.feedback_item
            self.set_feedback(value, color, error=error)

    def change_language(self, _event=None):
        if self.busy or self.closing or self.pending_language:
            return
        language = SUPPORTED_LANGUAGES[self.language_select.current()]
        if language != self.language:
            self.pending_language = language
            if self.last_status:
                self.apply_status(self.last_status)
            else:
                self.apply_language(language)
                self.state_label.configure(text=self.t("idle"))
                self.detail_label.configure(text=self.t("idle_detail"))
                self.driver_label.configure(text=self.t("driver_check"))
            self.set_feedback("changing_language")
            self._set_buttons()
            self.perform(lambda: self.controller.set_language(language), "changing_language", "language_changed")

    def _build_interface(self):
        from PIL import ImageTk
        from .map_markers import marker_image
        tk, ttk, p = self.tk, self.ttk, self.px
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        common = {"font": ("Microsoft YaHei UI", 10), "padding": (p(18), p(11)),
                  "relief": "flat", "borderwidth": p(1), "focusthickness": p(1),
                  "focuscolor": MUTED, "anchor": "center"}
        styles = {
            "Primary.TButton": (ACCENT, BG, ACCENT, "#ff897d"),
            "Stop.TButton": (CARD, TEXT, BORDER, "#3b2e35"),
            "Map.TButton": (CARD, TEXT, CARD, "#223244"),
            "Quiet.TButton": (BG, MUTED, BG, CARD),
        }
        for name, (background, foreground, border, hover) in styles.items():
            style.configure(name, **common, background=background, foreground=foreground,
                            bordercolor=border, lightcolor=border, darkcolor=border)
            style.map(name, background=[("disabled", "#202c3a"), ("pressed", hover), ("active", hover)],
                      foreground=[("disabled", "#617287")], bordercolor=[("disabled", BORDER), ("focus", MUTED)],
                      lightcolor=[("disabled", BORDER)], darkcolor=[("disabled", BORDER)])
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Stop.TButton", font=("Microsoft YaHei UI", 11))
        style.map("Stop.TButton", foreground=[("disabled", "#617287"), ("active", ACCENT)])
        style.configure("Map.TButton", font=("Microsoft YaHei UI", 12, "bold"), anchor="w",
                        padding=(p(4), p(10)))
        style.configure("Quiet.TButton", padding=(p(12), p(8)))
        style.configure("Language.TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT,
                        arrowcolor=MUTED, bordercolor=BORDER, padding=p(5))
        style.map("Language.TCombobox", fieldbackground=[("readonly", CARD)],
                  foreground=[("readonly", TEXT)], selectbackground=[("readonly", CARD)],
                  selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.font", ("Microsoft YaHei UI", 10))
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)

        frame = tk.Frame(self.root, bg=BG, padx=p(32), pady=p(22))
        frame.pack(fill="both", expand=True)
        self.content = frame
        header = tk.Frame(frame, bg=BG)
        header.pack(fill="x")
        self.brand_photo = ImageTk.PhotoImage(marker_image("seen", min(96, p(46))))
        mark = tk.Label(header, image=self.brand_photo, bg=BG, borderwidth=0)
        mark.pack(side="left", padx=(0, p(14)))
        title = tk.Frame(header, bg=BG)
        title.pack(side="left", fill="x", expand=True)
        tk.Label(title, text="AION2  /  HIDDEN CUBE RADAR", anchor="w", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=MUTED).pack(fill="x")
        self.localized(tk.Label(title, anchor="w", font=("Microsoft YaHei UI", 21, "bold"),
                                bg=BG, fg=TEXT), "title").pack(fill="x", pady=(p(1), 0))
        preferences = tk.Frame(header, bg=BG)
        preferences.pack(side="right", anchor="n")
        tk.Label(preferences, text=f"v{__version__}", font=("Segoe UI", 10), bg=BG, fg=MUTED).pack(anchor="e")
        self.language_select = ttk.Combobox(preferences, values=LANGUAGE_NAMES, state="readonly", width=12,
                                           font=("Microsoft YaHei UI", 10), style="Language.TCombobox")
        self.language_select.current(SUPPORTED_LANGUAGES.index(self.language))
        self.language_select.pack(anchor="e", pady=(p(5), 0))
        self.language_select.bind("<<ComboboxSelected>>", self.change_language)

        card = tk.Frame(frame, bg=CARD, highlightthickness=p(1), highlightbackground=BORDER)
        card.pack(fill="x", pady=(p(20), 0))
        self.status_rail = tk.Frame(card, bg=BORDER, width=p(3))
        self.status_rail.pack(side="left", fill="y")
        interior = tk.Frame(card, bg=CARD, padx=p(20), pady=p(16))
        interior.pack(side="left", fill="both", expand=True)
        summary = tk.Frame(interior, bg=CARD)
        summary.pack(fill="x")
        summary.columnconfigure(0, weight=1)
        state_area = tk.Frame(summary, bg=CARD)
        state_area.grid(row=0, column=0, sticky="nsew")
        self.localized(tk.Label(state_area, anchor="w", bg=CARD, fg=MUTED,
                                font=("Microsoft YaHei UI", 9)), "receive_status").pack(fill="x")
        self.state_label = tk.Label(state_area, text=self.t("idle"), anchor="w", bg=CARD, fg=TEXT,
                                    font=("Microsoft YaHei UI", 17, "bold"))
        self.state_label.pack(fill="x", pady=(p(4), p(5)))
        self.detail_label = tk.Label(state_area, text=self.t("idle_detail"), anchor="w", justify="left",
                                     wraplength=p(440), bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.detail_label.pack(fill="x")
        count_area = tk.Frame(summary, bg=CARD, padx=p(20))
        count_area.grid(row=0, column=1, sticky="ne", padx=(p(20), 0))
        self.count_label = tk.Label(count_area, text="—", anchor="e", bg=CARD, fg=TEXT,
                                    font=("Segoe UI", 27, "bold"))
        self.count_label.pack(anchor="e")
        self.localized(tk.Label(count_area, anchor="e", bg=CARD, fg=MUTED,
                                font=("Microsoft YaHei UI", 9)), "points").pack(anchor="e")
        tk.Frame(interior, height=p(1), bg=BORDER).pack(fill="x", pady=(p(13), p(12)))
        actions = tk.Frame(interior, bg=CARD)
        actions.pack(fill="x")
        actions.columnconfigure(0, weight=2)
        actions.columnconfigure(1, weight=1)
        self.start_button = self.localized(ttk.Button(actions, style="Primary.TButton",
                                       command=lambda: self.perform(self.controller.start_receiving, "starting")), "start")
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, p(6)))
        self.stop_button = self.localized(ttk.Button(actions, style="Stop.TButton",
                                      command=lambda: self.perform(self.controller.stop_receiving, "stopping")), "stop")
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(p(6), 0))

        self.localized(tk.Label(frame, anchor="w", bg=BG, fg=TEXT,
                                font=("Microsoft YaHei UI", 10, "bold")), "map_views").pack(fill="x", pady=(p(18), p(10)))
        maps = tk.Frame(frame, bg=BG)
        maps.pack(fill="x")
        maps.columnconfigure((0, 1), weight=1, uniform="map")
        self.overlay_button = self._map_entry(maps, 0, "overlay", "overlay_description",
                                              lambda: self.perform(self.controller.show_overlay, "opening_overlay"))
        self.map_button = self._map_entry(maps, 1, "full_map", "map_description",
                                          lambda: self.perform(self.controller.open_map, "opening_map"))
        secondary = tk.Frame(frame, bg=BG)
        secondary.pack(fill="x", pady=(p(12), 0))
        self.history_button = self.localized(ttk.Button(secondary, style="Quiet.TButton",
                                         command=lambda: self.perform(self.controller.open_map, "opening_history")), "history")
        self.history_button.pack(side="left", padx=(0, p(6)))
        self.data_button = self.localized(ttk.Button(secondary, style="Quiet.TButton",
                                      command=lambda: self.perform(self.controller.open_data_directory, "opening_data")), "data_folder")
        self.data_button.pack(side="left", padx=p(6))
        self.import_button = self.localized(ttk.Button(secondary, style="Quiet.TButton", command=self.import_resources), "import")
        self.import_button.pack(side="left", padx=(p(6), 0))
        self.alert_settings_button = self.localized(ttk.Button(secondary, style="Quiet.TButton",
                                                   command=self.open_alert_settings), "alert_settings")
        self.alert_settings_button.pack(side="left", padx=(p(6), 0))
        self.buttons = [self.start_button, self.stop_button, self.overlay_button, self.map_button,
                        self.history_button, self.data_button, self.import_button, self.alert_settings_button]
        footer = tk.Frame(frame, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(p(14), 0))
        tk.Frame(footer, height=p(1), bg=BORDER).pack(fill="x", pady=(0, p(12)))
        driver = tk.Frame(footer, bg=BG)
        driver.pack(fill="x")
        self.driver_label = tk.Label(driver, text=self.t("driver_check"), anchor="w", bg=BG,
                                     fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.driver_label.pack(side="left", fill="x", expand=True)
        self.feedback = tk.Label(footer, text="", anchor="w", justify="left", wraplength=p(680), bg=BG,
                                 fg=MUTED, font=("Microsoft YaHei UI", 9), height=2)
        self.feedback.pack(fill="x", pady=(p(5), 0))

    def _map_entry(self, parent, column, title, description, command):
        tk, p = self.tk, self.px
        card = tk.Frame(parent, bg=CARD, highlightthickness=p(1), highlightbackground=BORDER,
                        padx=p(16), pady=p(10))
        card.grid(row=0, column=column, sticky="nsew", padx=(0, p(6)) if column == 0 else (p(6), 0))
        button = self.localized(self.ttk.Button(card, command=command, style="Map.TButton"), title)
        button.pack(fill="x")
        self.localized(tk.Label(card, bg=CARD, fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 9),
                                padx=p(4)), description).pack(fill="x", pady=(p(1), p(8)))
        return button

    def schedule_startup(self, *, start_receiving=False, show_overlay=False, minimized=False):
        if minimized:
            self.root.iconify()
        if start_receiving or show_overlay:
            self.root.after_idle(lambda: self.start_requested(start_receiving, show_overlay))

    def start_requested(self, start_receiving, show_overlay):
        if self.closing:
            return

        def action():
            if self.closing:
                return
            if start_receiving:
                self.controller.start_receiving()
            if show_overlay and not self.closing:
                self.controller.show_overlay()

        self.perform(action, "starting_overlay" if start_receiving and show_overlay
                     else "starting" if start_receiving else "opening_overlay")

    def _set_buttons(self):
        for button in self.buttons:
            button.configure(state="disabled" if self.busy or self.closing or self.pending_language else "normal")
        self.language_select.configure(state="disabled" if self.busy or self.closing or self.pending_language else "readonly")
        if not self.busy and not self.closing and not self.pending_language:
            active = (self.last_status.get("mode") == "live" and not self.last_status.get("paused")
                      and self.last_status.get("phase") not in ("error", "stopped"))
            self.start_button.configure(state="disabled" if active or self.last_status.get("npcap") is False else "normal")
            self.stop_button.configure(state="normal" if active else "disabled")

    def apply_status(self, status):
        self.last_status = status
        self.apply_language(self.pending_language or status.get("language", self.language))
        if not status["connected"]:
            label, detail, color = "idle", "idle_detail", TEXT
        elif status["mode"] != "live":
            label, detail, color = "browsing", "browsing_detail", MUTED
        elif status["paused"]:
            label, detail, color = "paused", "paused_detail", MUTED
        elif status["phase"] == "error":
            label, detail, color = "unavailable", "unavailable_detail", ACCENT
        else:
            label = "receiving" if status["phase"] == "observing" else "waiting"
            detail = "receiving_detail" if status["phase"] == "observing" else "waiting_detail"
            color = RECEIVING if status["phase"] == "observing" else WARNING
        detail = self.t(detail)
        if status.get("storage_error"):
            detail += " " + self.t("storage_error")
        self.state_label.configure(text=self.t(label), fg=color)
        self.status_rail.configure(bg=color if color != TEXT else BORDER)
        self.detail_label.configure(text=detail, fg=WARNING if status.get("storage_error") else MUTED)
        self.count_label.configure(text="—" if status["record_count"] is None else f"{status['record_count']:,}")
        self.driver_label.configure(text=self.t("driver_ready" if status["npcap"] else "driver_missing"),
                                    fg=MUTED if status["npcap"] else WARNING)
        if status.get("language_save_error"):
            self.set_feedback("language_save_error", WARNING)
        self._set_buttons()

    def perform(self, function, message, success=""):
        if self.busy or self.closing:
            return
        if self.polling:
            self.root.after(100, lambda: self.perform(function, message, success))
            return
        self.busy = True
        self.set_feedback(message)
        self._set_buttons()

        def work():
            try:
                function()
                self.messages.put(("action", self.controller.status(), success))
            except Exception as exc:
                self.messages.put(("error", exc, None))
        threading.Thread(target=work, daemon=True, name="desktop-action").start()

    def import_resources(self):
        if self.busy or self.closing:
            return
        folder = self.filedialog.askdirectory(parent=self.root, title=self.t("import_title"))
        if folder:
            self.perform(lambda: self.controller.import_assets(folder), "importing", "imported")

    def open_alert_settings(self):
        from .desktop_alerts import AlertSettingsDialog
        dialog = getattr(self, "alert_dialog", None)
        if dialog is not None and dialog.root.winfo_exists():
            dialog.root.lift()
            return
        self.alert_dialog = AlertSettingsDialog(self.root, self.controller, self.language)

    def poll(self):
        if self.closing:
            return
        if not self.busy and not self.polling:
            self.polling = True
            def work():
                try:
                    self.messages.put(("status", self.controller.status(), None))
                except Exception as exc:
                    self.messages.put(("status_error", exc, None))
            threading.Thread(target=work, daemon=True, name="desktop-status").start()
        self.root.after(1000, self.poll)

    def drain(self):
        try:
            while True:
                kind, value, message = self.messages.get_nowait()
                if kind == "closed":
                    if value:
                        self.messagebox.showwarning(self.t("exit_title"), self.t(value), parent=self.root)
                    self.root.destroy()
                    return
                if kind in ("status", "status_error"):
                    self.polling = False
                if kind in ("action", "error"):
                    self.busy = False
                    self.pending_language = None
                if kind in ("status", "action"):
                    self.apply_status(value)
                    if kind == "action":
                        if not value.get("language_save_error"):
                            self.set_feedback(message or "done", RECEIVING)
                else:
                    if kind == "error" and self.last_status:
                        self.apply_status(self.last_status)
                    self.set_feedback(value, ACCENT, error=True)
                    self.language_select.current(SUPPORTED_LANGUAGES.index(self.language))
                    self._set_buttons()
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.set_feedback("closing")
        self._set_buttons()
        self._close_when_idle()

    def _close_when_idle(self):
        if self.busy or self.polling:
            self.root.after(100, self._close_when_idle)
            return
        self.set_feedback("closing")
        def work():
            try:
                self.controller.close()
                self.messages.put(("closed", None, None))
            except Exception:
                self.messages.put(("closed", "close_error", None))
        threading.Thread(target=work, daemon=True, name="desktop-close").start()

    def run(self):
        self.root.mainloop()


def run(port=18726, data_dir=None, *, start_receiving=False, show_overlay=False, minimized=False):
    DesktopWindow(RuntimeController(port, data_dir), start_receiving=start_receiving,
                  show_overlay=show_overlay, minimized=minimized).run()
    return 0
