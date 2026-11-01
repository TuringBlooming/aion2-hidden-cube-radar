"""The single user-facing Windows management window; collection is opt-in."""
from __future__ import annotations

import queue
from pathlib import Path
import threading
import webbrowser

from . import __version__
from .runtime import DesktopError, NPCAP_URL, RuntimeController

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
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.scale = ui_scale(self.root)
        self.px = lambda value: round(value * self.scale)
        self.root.title("AION2 RedBag Map")
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
        tk.Label(title, text="AION2  /  RED BAG MAP", anchor="w", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=MUTED).pack(fill="x")
        tk.Label(title, text="红色背包地图", anchor="w", font=("Microsoft YaHei UI", 21, "bold"),
                 bg=BG, fg=TEXT).pack(fill="x", pady=(p(1), 0))
        tk.Label(header, text=f"v{__version__}", font=("Segoe UI", 10), bg=BG, fg=MUTED).pack(side="right", anchor="n", pady=p(7))

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
        tk.Label(state_area, text="接收状态", anchor="w", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(fill="x")
        self.state_label = tk.Label(state_area, text="尚未开始接收", anchor="w", bg=CARD, fg=TEXT,
                                    font=("Microsoft YaHei UI", 17, "bold"))
        self.state_label.pack(fill="x", pady=(p(4), p(5)))
        self.detail_label = tk.Label(state_area, text="先浏览地图，或开启实时接收。", anchor="w", justify="left",
                                     wraplength=p(440), bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.detail_label.pack(fill="x")
        count_area = tk.Frame(summary, bg=CARD, padx=p(20))
        count_area.grid(row=0, column=1, sticky="ne", padx=(p(20), 0))
        self.count_label = tk.Label(count_area, text="—", anchor="e", bg=CARD, fg=TEXT,
                                    font=("Segoe UI", 27, "bold"))
        self.count_label.pack(anchor="e")
        tk.Label(count_area, text="观测点位", anchor="e", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="e")
        tk.Frame(interior, height=p(1), bg=BORDER).pack(fill="x", pady=(p(13), p(12)))
        actions = tk.Frame(interior, bg=CARD)
        actions.pack(fill="x")
        actions.columnconfigure(0, weight=2)
        actions.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(actions, text="开始实时接收", style="Primary.TButton",
                                       command=lambda: self.perform(self.controller.start_receiving, "正在开始接收…"))
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, p(6)))
        self.stop_button = ttk.Button(actions, text="停止接收", style="Stop.TButton",
                                      command=lambda: self.perform(self.controller.stop_receiving, "正在停止接收…"))
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(p(6), 0))

        tk.Label(frame, text="选择地图视图", anchor="w", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(fill="x", pady=(p(18), p(10)))
        maps = tk.Frame(frame, bg=BG)
        maps.pack(fill="x")
        maps.columnconfigure((0, 1), weight=1, uniform="map")
        self.overlay_button = self._map_entry(maps, 0, "显示悬浮地图  ↗", "置顶小窗，边跑图边查看",
                                              lambda: self.perform(self.controller.show_overlay, "正在打开悬浮地图…"))
        self.map_button = self._map_entry(maps, 1, "打开完整地图  ↗", "全图浏览，查看点位与记录",
                                          lambda: self.perform(self.controller.open_map, "正在打开完整地图…"))
        secondary = tk.Frame(frame, bg=BG)
        secondary.pack(fill="x", pady=(p(12), 0))
        self.history_button = ttk.Button(secondary, text="历史记录", style="Quiet.TButton",
                                         command=lambda: self.perform(self.controller.open_map, "正在打开历史记录…"))
        self.history_button.pack(side="left", padx=(0, p(6)))
        self.data_button = ttk.Button(secondary, text="数据目录", style="Quiet.TButton",
                                      command=lambda: self.perform(self.controller.open_data_directory, "正在打开数据目录…"))
        self.data_button.pack(side="left", padx=p(6))
        self.import_button = ttk.Button(secondary, text="导入地图资源", style="Quiet.TButton", command=self.import_resources)
        self.import_button.pack(side="left", padx=(p(6), 0))
        self.buttons = [self.start_button, self.stop_button, self.overlay_button, self.map_button,
                        self.history_button, self.data_button, self.import_button]
        footer = tk.Frame(frame, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(p(14), 0))
        tk.Frame(footer, height=p(1), bg=BORDER).pack(fill="x", pady=(0, p(12)))
        driver = tk.Frame(footer, bg=BG)
        driver.pack(fill="x")
        self.driver_label = tk.Label(driver, text="正在检查接收组件…", anchor="w", bg=BG,
                                     fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.driver_label.pack(side="left", fill="x", expand=True)
        self.driver_link = tk.Button(driver, text="Npcap 官方网站 ↗", command=lambda: webbrowser.open(NPCAP_URL),
                                     bg=BG, fg=MUTED, activebackground=BG, activeforeground=ACCENT,
                                     relief="flat", borderwidth=0, cursor="hand2", highlightthickness=0,
                                     font=("Microsoft YaHei UI", 9), padx=p(4), pady=p(3))
        self.driver_link.pack(side="right")
        self.feedback = tk.Label(footer, text="", anchor="w", justify="left", wraplength=p(680), bg=BG,
                                 fg=MUTED, font=("Microsoft YaHei UI", 9), height=2)
        self.feedback.pack(fill="x", pady=(p(5), 0))

    def _map_entry(self, parent, column, title, description, command):
        tk, p = self.tk, self.px
        card = tk.Frame(parent, bg=CARD, highlightthickness=p(1), highlightbackground=BORDER,
                        padx=p(16), pady=p(10))
        card.grid(row=0, column=column, sticky="nsew", padx=(0, p(6)) if column == 0 else (p(6), 0))
        button = self.ttk.Button(card, text=title, command=command, style="Map.TButton")
        button.pack(fill="x")
        tk.Label(card, text=description, bg=CARD, fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 9),
                 padx=p(4)).pack(fill="x", pady=(p(1), p(8)))
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

        self.perform(action, "正在开始接收并打开悬浮地图…" if start_receiving and show_overlay
                     else "正在开始接收…" if start_receiving else "正在打开悬浮地图…")

    def _set_buttons(self):
        for button in self.buttons:
            button.configure(state="disabled" if self.busy or self.closing else "normal")
        if not self.busy and not self.closing:
            active = (self.last_status.get("mode") == "live" and not self.last_status.get("paused")
                      and self.last_status.get("phase") not in ("error", "stopped"))
            self.start_button.configure(state="disabled" if active or self.last_status.get("npcap") is False else "normal")
            self.stop_button.configure(state="normal" if active else "disabled")

    def apply_status(self, status):
        self.last_status = status
        if not status["connected"]:
            label, detail, color = "尚未开始接收", "先浏览地图，或开启实时接收。", TEXT
        elif status["mode"] != "live":
            label, detail, color = "正在浏览历史", "查看已保存的背包点位与记录。", MUTED
        elif status["paused"]:
            label, detail, color = "接收已停止", "记录已保留，可随时继续接收。", MUTED
        elif status["phase"] == "error":
            label, detail, color = "接收暂不可用", status["detail"] or "请查看数据目录中的日志。", ACCENT
        else:
            label = "正在接收" if status["phase"] == "observing" else "正在等待记录"
            detail = "新背包与状态变化会自动更新到地图。" if status["phase"] == "observing" else "等待本机收到游戏记录。"
            color = RECEIVING if status["phase"] == "observing" else WARNING
        if status.get("storage_error"):
            detail += " 存档暂未保存，请检查数据目录。"
        self.state_label.configure(text=label, fg=color)
        self.status_rail.configure(bg=color if color != TEXT else BORDER)
        self.detail_label.configure(text=detail, fg=WARNING if status.get("storage_error") else MUTED)
        self.count_label.configure(text="—" if status["record_count"] is None else f"{status['record_count']:,}")
        self.driver_label.configure(text="接收组件已找到" if status["npcap"] else "未找到 Npcap，仍可浏览地图和历史。",
                                    fg=MUTED if status["npcap"] else WARNING)
        self._set_buttons()

    def perform(self, function, message, success=""):
        if self.busy or self.closing:
            return
        if self.polling:
            self.root.after(100, lambda: self.perform(function, message, success))
            return
        self.busy = True
        self.feedback.configure(text=message, fg=MUTED)
        self._set_buttons()

        def work():
            try:
                function()
                self.messages.put(("action", self.controller.status(), success))
            except Exception as exc:
                self.messages.put(("error", str(exc) if isinstance(exc, (DesktopError, ValueError, OSError)) else "操作未完成，请查看数据目录中的日志。", None))
        threading.Thread(target=work, daemon=True, name="desktop-action").start()

    def import_resources(self):
        if self.busy or self.closing:
            return
        folder = self.filedialog.askdirectory(parent=self.root, title="选择有权使用的地图资源目录")
        if folder:
            self.perform(lambda: self.controller.import_assets(folder), "正在导入地图资源…",
                         "地图资源已导入，重新打开地图窗口后生效。")

    def poll(self):
        if self.closing:
            return
        if not self.busy and not self.polling:
            self.polling = True
            def work():
                try:
                    self.messages.put(("status", self.controller.status(), None))
                except Exception as exc:
                    self.messages.put(("status_error", str(exc) if isinstance(exc, DesktopError) else "暂时无法读取本机地图状态。", None))
            threading.Thread(target=work, daemon=True, name="desktop-status").start()
        self.root.after(1000, self.poll)

    def drain(self):
        try:
            while True:
                kind, value, message = self.messages.get_nowait()
                if kind == "closed":
                    if value:
                        self.messagebox.showwarning("退出提示", value, parent=self.root)
                    self.root.destroy()
                    return
                if kind in ("status", "status_error"):
                    self.polling = False
                if kind in ("action", "error"):
                    self.busy = False
                if kind in ("status", "action"):
                    self.apply_status(value)
                    if kind == "action":
                        self.feedback.configure(text=message or "操作已完成。", fg=RECEIVING)
                else:
                    self.feedback.configure(text=value, fg=ACCENT)
                    self._set_buttons()
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.feedback.configure(text="正在保存并退出…", fg=MUTED)
        self._set_buttons()
        self._close_when_idle()

    def _close_when_idle(self):
        if self.busy or self.polling:
            self.root.after(100, self._close_when_idle)
            return
        self.feedback.configure(text="正在保存并退出…", fg=MUTED)
        def work():
            try:
                self.controller.close()
                self.messages.put(("closed", None, None))
            except Exception:
                self.messages.put(("closed", "部分本机窗口未能正常退出，请关闭仍显示的地图窗口。", None))
        threading.Thread(target=work, daemon=True, name="desktop-close").start()

    def run(self):
        self.root.mainloop()


def run(port=18726, data_dir=None, *, start_receiving=False, show_overlay=False, minimized=False):
    DesktopWindow(RuntimeController(port, data_dir), start_receiving=start_receiving,
                  show_overlay=show_overlay, minimized=minimized).run()
    return 0
