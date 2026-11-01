"""The single user-facing Windows management window; collection is opt-in."""
from __future__ import annotations

import queue
from pathlib import Path
import threading
import webbrowser

from . import __version__
from .runtime import DesktopError, NPCAP_URL, RuntimeController


class DesktopWindow:
    def __init__(self, controller):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.ttk, self.filedialog, self.messagebox = tk, ttk, filedialog, messagebox
        self.controller = controller
        self.messages = queue.Queue()
        self.busy = self.polling = self.closing = False
        self.last_status = {}
        self.root = tk.Tk()
        self.root.title("AION2 RedBag Map")
        icon = Path(__file__).resolve().parent / "redbag.ico"
        if icon.is_file():
            try:
                self.root.iconbitmap(str(icon))
            except tk.TclError:
                pass
        self.root.geometry("720x580")
        self.root.minsize(700, 560)
        self.root.configure(bg="#f3f5f8")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(15, 10))
        style.configure("Primary.TButton", foreground="white", background="#2365b4")
        style.map("Primary.TButton", background=[("active", "#1d5597"), ("disabled", "#b1bfce")])
        frame = tk.Frame(self.root, bg="#f3f5f8", padx=28, pady=22)
        frame.pack(fill="both", expand=True)
        header = tk.Frame(frame, bg="#f3f5f8")
        header.pack(fill="x")
        tk.Label(header, text="红色背包地图", font=("Microsoft YaHei UI", 22, "bold"),
                 bg="#f3f5f8", fg="#182b43").pack(side="left")
        tk.Label(header, text=f"v{__version__}", font=("Segoe UI", 10), bg="#f3f5f8", fg="#63748a").pack(side="right")
        tk.Label(frame, text="接收记录、悬浮查看和历史存档，在这里管理。", anchor="w",
                 font=("Microsoft YaHei UI", 10), bg="#f3f5f8", fg="#63748a").pack(fill="x", pady=(4, 18))
        card = tk.Frame(frame, bg="white", padx=18, pady=15, highlightthickness=1, highlightbackground="#e0e5ec")
        card.pack(fill="x")
        self.state_label = tk.Label(card, text="尚未开始接收", anchor="w", bg="white", fg="#182b43",
                                    font=("Microsoft YaHei UI", 14, "bold"))
        self.state_label.pack(fill="x")
        self.detail_label = tk.Label(card, text="点击开始后才会接收；也可以直接浏览历史。", anchor="w", justify="left",
                                     wraplength=600, bg="white", fg="#63748a", font=("Microsoft YaHei UI", 9))
        self.detail_label.pack(fill="x", pady=(6, 0))
        actions = tk.Frame(frame, bg="#f3f5f8")
        actions.pack(fill="x", pady=(18, 10))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(actions, text="开始实时接收", style="Primary.TButton",
                                       command=lambda: self.perform(self.controller.start_receiving, "正在开始接收…"))
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 5), pady=4)
        self.stop_button = ttk.Button(actions, text="停止接收",
                                      command=lambda: self.perform(self.controller.stop_receiving, "正在停止接收…"))
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=4)
        self.overlay_button = ttk.Button(actions, text="显示悬浮地图",
                                         command=lambda: self.perform(self.controller.show_overlay, "正在打开悬浮地图…"))
        self.overlay_button.grid(row=1, column=0, sticky="ew", padx=(0, 5), pady=4)
        self.map_button = ttk.Button(actions, text="打开完整地图",
                                     command=lambda: self.perform(self.controller.open_map, "正在打开完整地图…"))
        self.map_button.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=4)
        secondary = tk.Frame(frame, bg="#f3f5f8")
        secondary.pack(fill="x")
        self.history_button = ttk.Button(secondary, text="浏览历史",
                                         command=lambda: self.perform(self.controller.open_map, "正在打开历史记录…"))
        self.history_button.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.data_button = ttk.Button(secondary, text="打开数据目录",
                                      command=lambda: self.perform(self.controller.open_data_directory, "正在打开数据目录…"))
        self.data_button.pack(side="left", fill="x", expand=True, padx=5)
        self.import_button = ttk.Button(secondary, text="导入地图资源", command=self.import_resources)
        self.import_button.pack(side="left", fill="x", expand=True, padx=(5, 0))
        self.buttons = [self.start_button, self.stop_button, self.overlay_button, self.map_button,
                        self.history_button, self.data_button, self.import_button]
        self.notice = tk.Label(frame, text="已内置地图与候选点；拖动地图可自由浏览", anchor="w", bg="#f3f5f8",
                               fg="#63748a", font=("Microsoft YaHei UI", 9))
        self.notice.pack(fill="x", pady=(13, 8))
        driver = tk.Frame(frame, bg="#f3f5f8")
        driver.pack(fill="x")
        self.driver_label = tk.Label(driver, text="正在检查接收组件…", anchor="w", bg="#f3f5f8",
                                     fg="#63748a", font=("Microsoft YaHei UI", 9))
        self.driver_label.pack(side="left", fill="x", expand=True)
        self.driver_link = tk.Button(driver, text="Npcap 官方网站 ↗", command=lambda: webbrowser.open(NPCAP_URL),
                                     bg="#f3f5f8", fg="#2365b4", relief="flat", cursor="hand2",
                                     font=("Microsoft YaHei UI", 9), padx=0)
        self.driver_link.pack(side="right")
        self.feedback = tk.Label(frame, text="", anchor="w", justify="left", wraplength=620, bg="#f3f5f8",
                                 fg="#2365b4", font=("Microsoft YaHei UI", 9))
        self.feedback.pack(fill="x", pady=(10, 0))
        self._set_buttons()
        self.root.after(100, self.drain)
        self.root.after(50, self.poll)

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
            label, detail = "尚未开始接收", "点击开始后才会接收；也可以直接浏览历史。"
        elif status["mode"] != "live":
            label, detail = "正在浏览历史", "历史模式没有启动实时接收。"
        elif status["paused"]:
            label, detail = "接收已停止", "地图与已保存记录仍可查看。点击开始可继续接收。"
        elif status["phase"] == "error":
            label, detail = "接收暂不可用", status["detail"] or "请查看本机数据目录中的日志。"
        else:
            label = "正在接收" if status["phase"] == "observing" else "正在等待记录"
            detail = status["detail"] or "等待本机收到游戏记录。"
        if status["record_count"] is not None:
            detail += f"  已记录 {status['record_count']} 个点位。"
        if status.get("storage_error"):
            detail += " 存档暂未保存，请检查数据目录。"
        self.state_label.configure(text=label)
        self.detail_label.configure(text=detail)
        self.driver_label.configure(text="接收组件已找到" if status["npcap"] else "未找到 Npcap，仍可浏览地图和历史。",
                                    fg="#63748a" if status["npcap"] else "#a66a18")
        self._set_buttons()

    def perform(self, function, message, success=""):
        if self.busy or self.closing:
            return
        if self.polling:
            self.root.after(100, lambda: self.perform(function, message, success))
            return
        self.busy = True
        self.feedback.configure(text=message, fg="#2365b4")
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
                        self.feedback.configure(text=message or "操作已完成。", fg="#2365b4")
                else:
                    self.feedback.configure(text=value, fg="#a53832")
                    self._set_buttons()
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.feedback.configure(text="正在保存并退出…", fg="#2365b4")
        self._set_buttons()
        self._close_when_idle()

    def _close_when_idle(self):
        if self.busy or self.polling:
            self.root.after(100, self._close_when_idle)
            return
        self.feedback.configure(text="正在保存并退出…", fg="#2365b4")
        def work():
            try:
                self.controller.close()
                self.messages.put(("closed", None, None))
            except Exception:
                self.messages.put(("closed", "部分本机窗口未能正常退出，请关闭仍显示的地图窗口。", None))
        threading.Thread(target=work, daemon=True, name="desktop-close").start()

    def run(self):
        self.root.mainloop()


def run(port=18726, data_dir=None):
    DesktopWindow(RuntimeController(port, data_dir)).run()
    return 0
