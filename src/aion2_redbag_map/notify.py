"""Windows notification-area balloons and sound; no game-window interaction."""
import ctypes as C
from ctypes import wintypes as W
import logging
import os
from pathlib import Path
import queue
import threading
import time
import webbrowser

from .localization import DEFAULT_LANGUAGE, text as localized_text, validate_language


class Notifier:
    def __init__(self, url, language=DEFAULT_LANGUAGE):
        self.url = url
        self.language = validate_language(language)
        self.queue = queue.Queue(maxsize=64)
        self.stopping = threading.Event()
        self.sound_path = Path(__file__).resolve().parent / "static" / "alert.wav"
        self.status = {"available": False, "sent": 0, "last_at": None, "error": None,
                       "sound_name": "三组双音警铃", "sound_requested": 0,
                       "sound_last_at": None, "sound_last_accepted": None, "sound_error": None}
        self.thread = threading.Thread(target=self._run, name="desktop-notifications", daemon=True)

    def start(self):
        self.thread.start()

    def set_language(self, language):
        # The worker updates the tray tooltip on its own thread; no alert or sound.
        self.language = validate_language(language)

    def close(self):
        self.stopping.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def send(self, alert, sound=True):
        try:
            self.queue.put_nowait((dict(alert), bool(sound)))
        except queue.Full:
            self.status["error"] = "提醒过于密集，请查看地图中的完整记录"

    def _run(self):
        if os.name != "nt":
            return
        try:
            self._windows_loop()
        except Exception as exc:
            self.status.update(available=False, error=f"系统通知不可用：{type(exc).__name__}")
            logging.exception("Notification worker stopped")

    def _play_sound(self):
        """Submit our local alarm to Windows; acceptance does not prove audibility."""
        self.status.update(sound_requested=self.status["sound_requested"] + 1,
                           sound_last_at=time.time(), sound_last_accepted=False, sound_error=None)
        try:
            if not self.sound_path.is_file():
                raise FileNotFoundError(self.sound_path)
            import winsound
            # NODEFAULT prevents Windows from substituting a system chime on failure.
            winsound.PlaySound(str(self.sound_path),
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except (ImportError, OSError, RuntimeError) as exc:
            self.status["sound_error"] = f"警铃未能播放：{type(exc).__name__}。请检查工具音量或警铃文件。"
            logging.exception("Alarm playback request failed")
            return False
        self.status["sound_last_accepted"] = True
        return True

    def _windows_loop(self):
        user = C.WinDLL("user32", use_last_error=True)
        shell = C.WinDLL("shell32", use_last_error=True)
        kernel = C.WinDLL("kernel32", use_last_error=True)
        LRESULT = C.c_ssize_t
        WNDPROC = C.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

        class WNDCLASS(C.Structure):
            _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", C.c_int),
                        ("cbWndExtra", C.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                        ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                        ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]

        class GUID(C.Structure):
            _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", C.c_ubyte * 8)]

        class NID(C.Structure):
            _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT),
                        ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", W.WCHAR * 128),
                        ("dwState", W.DWORD), ("dwStateMask", W.DWORD), ("szInfo", W.WCHAR * 256),
                        ("uTimeoutOrVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64),
                        ("dwInfoFlags", W.DWORD), ("guidItem", GUID), ("hBalloonIcon", W.HICON)]

        kernel.GetModuleHandleW.argtypes = [W.LPCWSTR]
        kernel.GetModuleHandleW.restype = W.HMODULE
        user.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        user.DefWindowProcW.restype = LRESULT
        user.RegisterClassW.argtypes = [C.POINTER(WNDCLASS)]
        user.RegisterClassW.restype = W.ATOM
        user.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int,
                                         C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
        user.CreateWindowExW.restype = W.HWND
        user.LoadIconW.argtypes = [W.HINSTANCE, W.LPCWSTR]
        user.LoadIconW.restype = W.HICON
        user.DestroyWindow.argtypes = [W.HWND]
        user.UnregisterClassW.argtypes = [W.LPCWSTR, W.HINSTANCE]
        user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
        user.DispatchMessageW.argtypes = [C.POINTER(W.MSG)]
        user.DispatchMessageW.restype = LRESULT
        shell.Shell_NotifyIconW.argtypes = [W.DWORD, C.POINTER(NID)]
        shell.Shell_NotifyIconW.restype = W.BOOL
        wm_callback = 0x8000 + 17

        @WNDPROC
        def window_proc(hwnd, message, wparam, lparam):
            if message == wm_callback and (lparam & 0xffff) in (0x0202, 0x0405):
                # Clicking the tray icon or a balloon opens the local map.
                webbrowser.open(self.url)
                return 0
            return user.DefWindowProcW(hwnd, message, wparam, lparam)

        instance = kernel.GetModuleHandleW(None)
        name = f"RedBagMapNotifications-{os.getpid()}"
        wc = WNDCLASS(0, window_proc, 0, 0, instance, None, None, None, None, name)
        if not user.RegisterClassW(C.byref(wc)):
            raise C.WinError(C.get_last_error())
        hwnd = user.CreateWindowExW(0, name, name, 0, 0, 0, 0, 0, None, None, instance, None)
        if not hwnd:
            raise C.WinError(C.get_last_error())
        nid = NID()
        nid.cbSize = C.sizeof(NID)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = 1 | 2 | 4
        nid.uCallbackMessage = wm_callback
        nid.hIcon = user.LoadIconW(None, C.cast(C.c_void_p(32516), W.LPCWSTR))
        tray_language = self.language
        nid.szTip = localized_text("tray", tray_language)[:127]
        added = bool(shell.Shell_NotifyIconW(0, C.byref(nid)))
        self.status["available"] = added
        if not added:
            self.status["error"] = "Windows 未接受托盘图标，仍可在页面查看提醒"
        try:
            message = W.MSG()
            while not self.stopping.is_set():
                if tray_language != self.language:
                    tray_language = self.language
                    nid.uFlags = 4
                    nid.szTip = localized_text("tray", tray_language)[:127]
                    if added:
                        shell.Shell_NotifyIconW(1, C.byref(nid))
                while user.PeekMessageW(C.byref(message), None, 0, 0, 1):
                    user.DispatchMessageW(C.byref(message))
                try:
                    alert, sound = self.queue.get(timeout=0.08)
                except queue.Empty:
                    continue
                if sound:
                    self._play_sound()
                nid.uFlags = 0x10
                nid.szInfoTitle = alert.get("title", localized_text("new_bag", self.language))[:63]
                nid.szInfo = alert.get("text", localized_text("view_map", self.language))[:255]
                nid.dwInfoFlags = 1 | 0x10  # We control the sound separately.
                nid.uTimeoutOrVersion = 10000
                accepted = bool(shell.Shell_NotifyIconW(1, C.byref(nid))) if added else False
                self.status.update(sent=self.status["sent"] + 1, last_at=time.time(), last_accepted=accepted)
        finally:
            if added:
                shell.Shell_NotifyIconW(2, C.byref(nid))
            user.DestroyWindow(hwnd)
            user.UnregisterClassW(name, instance)
