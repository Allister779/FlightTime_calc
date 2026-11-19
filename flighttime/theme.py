"""Light / dark appearance and DPI-aware sizing for the Tkinter interface.

The ``clam`` ttk theme is used on every platform because it is the built-in theme whose
colours can be fully restyled (the native Windows and macOS themes ignore most colours),
which is what makes a dark mode possible without third-party packages.
"""

import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

LIGHT = {
    "name": "light",
    "bg": "#f3f4f6", "surface": "#ffffff", "fg": "#1f2328", "muted": "#5c6670",
    "border": "#b8bec6", "field": "#ffffff", "select_bg": "#1f5fbf", "select_fg": "#ffffff",
    "button": "#e4e7eb", "button_active": "#d3d8de", "heading": "#e6e9ed",
    "tab_active_bg": "#1f5fbf", "tab_active_fg": "#ffffff", "tab_idle_bg": "#dfe3e8",
    "tab_idle_fg": "#2b2f33", "tab_hover_bg": "#c6ccd4",
    "warn": "#b35c00", "slower": "#a0182b", "faster": "#1d6b28", "bad_bg": "#f8d7da",
    "map_bg": "#eef1f4", "overlay_bg": "#ffffff", "overlay_fg": "#222222", "overlay_border": "#9aa3ad",
    "dial_face": "#ffffff", "dial_ring": "#888888", "dial_tick": "#999999",
}

DARK = {
    "name": "dark",
    "bg": "#1e2227", "surface": "#262b31", "fg": "#e6e8eb", "muted": "#9aa4ae",
    "border": "#454d57", "field": "#2d333b", "select_bg": "#2f6fd6", "select_fg": "#ffffff",
    "button": "#323840", "button_active": "#3d444d", "heading": "#2d333b",
    "tab_active_bg": "#2f6fd6", "tab_active_fg": "#ffffff", "tab_idle_bg": "#2d333b",
    "tab_idle_fg": "#d5d9de", "tab_hover_bg": "#3a414a",
    "warn": "#f0a14a", "slower": "#ff8590", "faster": "#6fdc88", "bad_bg": "#5a1d24",
    "map_bg": "#1a1d21", "overlay_bg": "#262b31", "overlay_fg": "#e6e8eb", "overlay_border": "#5a636e",
    "dial_face": "#2d333b", "dial_ring": "#8a949e", "dial_tick": "#6b7580",
}

APPEARANCES = ("System", "Light", "Dark")


def system_prefers_dark():
    """Best-effort detection of the operating system's dark mode setting."""
    try:
        if sys.platform == "win32":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=2)
            return "dark" in out.stdout.lower()
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=2)
        if "dark" in out.stdout.lower():
            return True
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                             capture_output=True, text=True, timeout=2)
        return "dark" in out.stdout.lower()
    except Exception:
        return False


def resolve(appearance):
    if appearance == "Dark":
        return DARK
    if appearance == "Light":
        return LIGHT
    return DARK if system_prefers_dark() else LIGHT


def ui_scale(root):
    """Display scale factor relative to a standard 96 DPI screen (1.0, 1.25, 1.5, 2.0 ...)."""
    try:
        return max(float(root.tk.call("tk", "scaling")) / (96.0 / 72.0), 1.0)
    except Exception:
        return 1.0


class Fonts:
    """Named fonts derived from the system default so they follow OS size and DPI settings."""

    def __init__(self, root):
        base = tkfont.nametofont("TkDefaultFont")
        fam, size = base.cget("family"), base.cget("size")
        self.normal = base
        self.bold = tkfont.Font(root, family=fam, size=size, weight="bold")
        self.small = tkfont.Font(root, family=fam, size=size - 1 if size > 0 else size + 1)
        self.small_bold = tkfont.Font(root, family=fam, size=size - 1 if size > 0 else size + 1, weight="bold")
        self.big = tkfont.Font(root, family=fam, size=size + 7 if size > 0 else size - 9, weight="bold")
        self.tab = tkfont.Font(root, family=fam, size=size + 1 if size > 0 else size - 1, weight="bold")

    def line(self, font=None):
        return (font or self.normal).metrics("linespace")


def set_windows_dark_titlebar(root, dark):
    """Ask Windows 10/11 to draw the title bar dark or light to match."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (newer / older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def apply(root, p, fonts, comboboxes=()):
    """Restyle all ttk widgets for palette ``p`` and size rows from the real font height."""
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    row = int(fonts.line() * 1.5) + 2
    heading_pad = max(int(fonts.line() * 0.3), 3)
    root.configure(background=p["bg"])

    style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"],
                    bordercolor=p["border"], darkcolor=p["bg"], lightcolor=p["bg"], troughcolor=p["surface"],
                    selectbackground=p["select_bg"], selectforeground=p["select_fg"], insertcolor=p["fg"],
                    focuscolor=p["select_bg"], arrowcolor=p["fg"], font=fonts.normal)
    style.map(".", background=[("disabled", p["bg"])], foreground=[("disabled", p["muted"])])
    style.configure("TFrame", background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["fg"])
    style.configure("Big.TLabel", font=fonts.big)
    style.configure("Caption.TLabel", foreground=p["muted"])
    style.configure("Warn.TLabel", foreground=p["warn"])
    style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
    style.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"], font=fonts.bold)
    pad = (max(int(fonts.line() * 0.6), 6), max(int(fonts.line() * 0.25), 3))
    style.configure("TButton", background=p["button"], foreground=p["fg"], bordercolor=p["border"],
                    lightcolor=p["button"], darkcolor=p["button"], padding=pad)
    style.map("TButton", background=[("disabled", p["bg"]), ("pressed", p["select_bg"]),
                                     ("active", p["button_active"])],
              foreground=[("disabled", p["muted"]), ("pressed", p["select_fg"])],
              lightcolor=[("active", p["button_active"])], darkcolor=[("active", p["button_active"])])
    for w in ("TCheckbutton", "TRadiobutton"):
        style.configure(w, background=p["bg"], foreground=p["fg"], indicatorbackground=p["field"],
                        indicatorforeground=p["fg"], upperbordercolor=p["border"], lowerbordercolor=p["border"])
        style.map(w, background=[("active", p["bg"])],
                  indicatorbackground=[("pressed", p["button_active"]), ("selected", p["field"])])
    style.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"], bordercolor=p["border"],
                    lightcolor=p["field"], darkcolor=p["field"], padding=2)
    style.map("TEntry", bordercolor=[("focus", p["select_bg"])], lightcolor=[("focus", p["select_bg"])])
    style.configure("TCombobox", fieldbackground=p["field"], foreground=p["fg"], background=p["button"],
                    bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"], arrowcolor=p["fg"],
                    padding=2)
    style.map("TCombobox", fieldbackground=[("readonly", p["field"]), ("disabled", p["bg"])],
              foreground=[("readonly", p["fg"]), ("disabled", p["muted"])],
              background=[("active", p["button_active"])],
              selectbackground=[("readonly", p["field"])], selectforeground=[("readonly", p["fg"])])
    style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["fg"],
                    bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"], rowheight=row,
                    font=fonts.normal)
    style.map("Treeview", background=[("selected", p["select_bg"])], foreground=[("selected", p["select_fg"])])
    style.configure("Treeview.Heading", background=p["heading"], foreground=p["fg"], font=fonts.normal,
                    bordercolor=p["border"], lightcolor=p["heading"], darkcolor=p["heading"],
                    padding=(4, heading_pad))
    style.map("Treeview.Heading", background=[("active", p["button_active"])])
    style.configure("TScrollbar", background=p["button"], troughcolor=p["bg"], bordercolor=p["border"],
                    lightcolor=p["button"], darkcolor=p["button"], arrowcolor=p["fg"])
    style.map("TScrollbar", background=[("active", p["button_active"])])
    style.configure("TPanedwindow", background=p["bg"])
    style.configure("Sash", background=p["border"], sashthickness=6, gripcount=0)

    # Drop-down lists of comboboxes are classic Tk listboxes.
    for opt, val in (("background", p["field"]), ("foreground", p["fg"]),
                     ("selectBackground", p["select_bg"]), ("selectForeground", p["select_fg"])):
        root.option_add("*TCombobox*Listbox." + opt, val)
    for cb in comboboxes:
        try:
            popdown = root.tk.call("ttk::combobox::PopdownWindow", cb)
            root.tk.call(str(popdown) + ".f.l", "configure", "-background", p["field"], "-foreground", p["fg"],
                         "-selectbackground", p["select_bg"], "-selectforeground", p["select_fg"])
        except tk.TclError:
            pass
    set_windows_dark_titlebar(root, p["name"] == "dark")
    return row
