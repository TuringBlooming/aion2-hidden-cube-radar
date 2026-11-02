"""An explicit draft/save editor for local alert preferences; no receiver startup."""
import queue
import threading

from .audio import DEFAULT_SOUND, DEFAULT_VOLUME, SOUND_IDS, validate_sound_settings
from .desktop_text import error_text, text
from .localization import restored_language
from .ui_theme import ui_scale

BG, CARD, BORDER = "#101722", "#182331", "#293747"
TEXT, MUTED, ACCENT, SUCCESS = "#edf2f7", "#91a2b5", "#ee7064", "#68d6ae"


class AlertSettingsDialog:
    def __init__(self, parent, controller, language="zh-CN"):
        import tkinter as tk
        from tkinter import ttk
        self.controller = controller
        self.language = restored_language(language)
        self.busy = self.closed = self._ready = False
        self.messages = queue.Queue()
        self.localized_widgets = []
        self.feedback_key, self.feedback_error = "alerts_draft", None
        values = dict(getattr(controller, "alert_settings", {}))
        try:
            profile, volume = validate_sound_settings(values.get("sound_profile", DEFAULT_SOUND),
                                                       values.get("sound_volume", DEFAULT_VOLUME))
        except ValueError:
            profile, volume = DEFAULT_SOUND, DEFAULT_VOLUME
        self.root = tk.Toplevel(parent)
        self.root.configure(bg=BG)
        self.root.transient(parent)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.close())
        self.scale = ui_scale(self.root)
        self.px = lambda value: round(value * self.scale)
        self.volume_var = tk.IntVar(self.root, value=volume)
        self.overlay_var = tk.BooleanVar(self.root, value=values.get("overlay_on_alert") is True)
        style = ttk.Style(self.root)
        style.configure("Alerts.TCombobox", fieldbackground=BG, background=BG, foreground=TEXT,
                        arrowcolor=MUTED, padding=self.px(6))
        style.map("Alerts.TCombobox", fieldbackground=[("readonly", BG)],
                  foreground=[("readonly", TEXT)], selectbackground=[("readonly", BG)],
                  selectforeground=[("readonly", TEXT)])
        for name, color, foreground in (("Alerts.Primary.TButton", ACCENT, BG),
                                         ("Alerts.Quiet.TButton", CARD, TEXT)):
            style.configure(name, background=color, foreground=foreground, borderwidth=0,
                            font=("Microsoft YaHei UI", 10), padding=(self.px(16), self.px(9)))
            style.map(name, background=[("disabled", BORDER), ("active", BORDER)],
                      foreground=[("disabled", MUTED)])
        frame = tk.Frame(self.root, bg=BG, padx=self.px(24), pady=self.px(20))
        frame.pack(fill="both", expand=True)
        self._label(tk.Label(frame, font=("Microsoft YaHei UI", 18, "bold"),
                             bg=BG, fg=TEXT, anchor="w"), "alert_settings").pack(fill="x")
        self._label(tk.Label(frame, font=("Microsoft YaHei UI", 10), bg=BG, fg=MUTED,
                             anchor="w", justify="left", wraplength=self.px(500)),
                    "alerts_subtitle").pack(fill="x", pady=(self.px(5), self.px(16)))
        card = tk.Frame(frame, bg=CARD, padx=self.px(18), pady=self.px(16),
                        highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x")
        self._label(tk.Label(card, bg=CARD, fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 9)),
                    "alerts_sound").pack(fill="x")
        self.sound_select = ttk.Combobox(card, state="readonly", style="Alerts.TCombobox",
                                         font=("Microsoft YaHei UI", 11))
        self.sound_select.pack(fill="x", pady=(self.px(5), self.px(16)))
        self.sound_select.bind("<<ComboboxSelected>>", self._draft_changed)
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x")
        self._label(tk.Label(row, bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 9)),
                    "alerts_volume").pack(side="left")
        self.volume_label = tk.Label(row, bg=CARD, fg=TEXT, font=("Segoe UI", 11, "bold"))
        self.volume_label.pack(side="right")
        self.volume_scale = tk.Scale(card, from_=0, to=100, orient="horizontal", resolution=1,
                                     showvalue=False, variable=self.volume_var, bg=CARD, fg=TEXT,
                                     troughcolor=BG, activebackground=ACCENT, highlightthickness=0,
                                     sliderrelief="flat", sliderlength=self.px(22), bd=0, width=self.px(9))
        self.volume_scale.pack(fill="x", pady=(self.px(5), self.px(6)))
        self._label(tk.Label(card, bg=CARD, fg=MUTED, anchor="w", justify="left",
                             font=("Microsoft YaHei UI", 9), wraplength=self.px(490)),
                    "alerts_volume_note").pack(fill="x")
        tk.Frame(card, bg=BORDER, height=self.px(1)).pack(fill="x", pady=self.px(16))
        self.overlay_check = self._label(tk.Checkbutton(card, variable=self.overlay_var, bg=CARD,
                                                       fg=TEXT, activebackground=CARD, activeforeground=TEXT,
                                                       selectcolor=BG, anchor="w", justify="left", bd=0,
                                                       font=("Microsoft YaHei UI", 10),
                                                       wraplength=self.px(465)), "alerts_overlay")
        self.overlay_check.pack(fill="x")
        self._label(tk.Label(card, bg=CARD, fg=MUTED, anchor="w", justify="left",
                             font=("Microsoft YaHei UI", 9), wraplength=self.px(490)),
                    "alerts_overlay_note").pack(fill="x", pady=(self.px(5), 0))
        self.feedback = tk.Label(frame, bg=BG, fg=MUTED, anchor="w", justify="left",
                                  font=("Microsoft YaHei UI", 9), wraplength=self.px(500))
        self.feedback.pack(fill="x", pady=(self.px(14), self.px(14)))
        actions = tk.Frame(frame, bg=BG)
        actions.pack(fill="x")
        self.preview_button = self._label(ttk.Button(actions, command=self.preview,
                                                     style="Alerts.Quiet.TButton"), "alerts_preview")
        self.preview_button.pack(side="left")
        self.save_button = self._label(ttk.Button(actions, command=self.save,
                                                  style="Alerts.Primary.TButton"), "alerts_save")
        self.save_button.pack(side="right")
        self.close_button = self._label(ttk.Button(actions, command=self.close,
                                                   style="Alerts.Quiet.TButton"), "alerts_close")
        self.close_button.pack(side="right", padx=self.px(8))
        self.set_language(self.language, profile=profile)
        self.volume_var.trace_add("write", self._draft_changed)
        self.overlay_var.trace_add("write", self._draft_changed)
        self._ready = True
        self._after_id = self.root.after(60, self.drain)

    def t(self, key):
        return text(key, self.language)

    def _label(self, widget, key):
        self.localized_widgets.append((widget, key))
        widget.configure(text=self.t(key))
        return widget

    def _draft_changed(self, *_args):
        self.volume_label.configure(text=f"{self.volume_var.get()}%")
        if self._ready and not self.busy:
            self.feedback_key, self.feedback_error = "alerts_draft", None
            self._show_feedback()

    def draft(self):
        index = self.sound_select.current()
        profile = SOUND_IDS[index] if 0 <= index < len(SOUND_IDS) else DEFAULT_SOUND
        profile, volume = validate_sound_settings(profile, self.volume_var.get())
        return {"sound_profile": profile, "sound_volume": volume, "overlay_on_alert": self.overlay_var.get()}

    def set_language(self, language, *, profile=None):
        if self.closed:
            return
        language = restored_language(language)
        if self._ready and language == self.language and profile is None:
            return
        if profile is None:
            index = self.sound_select.current()
            profile = SOUND_IDS[index] if 0 <= index < len(SOUND_IDS) else DEFAULT_SOUND
        self.language = language
        self.root.title(self.t("alert_settings"))
        for widget, key in self.localized_widgets:
            widget.configure(text=self.t(key))
        self.sound_select.configure(values=[self.t(f"sound_{item}") for item in SOUND_IDS])
        self.sound_select.current(SOUND_IDS.index(profile))
        self.volume_label.configure(text=f"{self.volume_var.get()}%")
        self._show_feedback()
        self.root.update_idletasks()
        self.root.geometry(f"{self.px(600)}x{max(self.px(490), self.root.winfo_reqheight())}")

    def _show_feedback(self):
        value = (error_text(self.feedback_error, self.language, fallback="alerts_failed")
                 if self.feedback_error is not None else self.t(self.feedback_key))
        self.feedback.configure(text=value, fg=ACCENT if self.feedback_error is not None else
                                SUCCESS if self.feedback_key == "alerts_saved" else MUTED)

    def _set_enabled(self):
        self.sound_select.configure(state="disabled" if self.busy else "readonly")
        for widget in (self.volume_scale, self.overlay_check, self.preview_button, self.save_button):
            widget.configure(state="disabled" if self.busy else "normal")

    def _perform(self, kind):
        if self.busy or self.closed:
            return
        values = self.draft()
        self.busy = True
        self.feedback_key, self.feedback_error = ("alerts_saving" if kind == "save" else "alerts_previewing"), None
        self._show_feedback()
        self._set_enabled()

        def work():
            try:
                result = (self.controller.set_alert_settings(**values) if kind == "save" else
                          self.controller.preview_sound(values["sound_profile"], values["sound_volume"]))
                if result is False and (kind == "save" or values["sound_volume"] != 0):
                    raise RuntimeError("Alert operation was not accepted")
                self.messages.put((kind, values["sound_volume"], None))
            except Exception as exc:
                self.messages.put((kind, values["sound_volume"], exc))
        threading.Thread(target=work, daemon=True, name=f"desktop-alert-{kind}").start()

    def preview(self):
        self._perform("preview")

    def save(self):
        self._perform("save")

    def drain(self):
        if self.closed:
            return
        try:
            while True:
                kind, volume, error = self.messages.get_nowait()
                self.busy = False
                self.feedback_error = error
                self.feedback_key = ("alerts_failed" if error is not None else "alerts_saved" if kind == "save"
                                     else "alerts_muted" if volume == 0 else "alerts_preview_sent")
                self._show_feedback()
                self._set_enabled()
        except queue.Empty:
            pass
        self._after_id = self.root.after(60, self.drain)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if getattr(self, "_after_id", None) is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
        self.root.destroy()
