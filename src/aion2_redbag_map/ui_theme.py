"""DPI setup for this application's own native windows."""
import ctypes
import math
import os


def enable_dpi_awareness():
    """Call before creating Tk; the packaged manifest establishes the same mode."""
    if os.name != "nt":
        return
    try:
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        api.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        # A false result also means the application manifest already set the mode.
        api.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def ui_scale(root):
    """Convert 96-DPI layout pixels to native pixels; Tk scales point fonts itself."""
    value = float(root.winfo_fpixels("1i")) / 96
    return max(.75, min(4.0, value)) if math.isfinite(value) else 1.0
