"""Tkinter desktop interface for FlightTime Calc."""

import csv
import json
import math
import os
import queue
import sys
import threading
import tkinter as tk
from collections import OrderedDict
from tkinter import filedialog, messagebox, ttk

from . import __version__
from . import basemap
from . import theme
from . import mission as M
from .aircraft import FIXED_WING, PRESETS, VTOL, AircraftProfile
from .estimator import direction_sweep, estimate
from .weather import WeatherError, fetch_nearest_metar, fetch_open_meteo
from .wind import WindLayer, WindProfile

APP_NAME = "FlightTime Calc"

SPEED_UNITS = {"kt": 0.514444, "m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704}
ALT_UNITS = {"m": 1.0, "ft": 0.3048}
DIST_FOR_SPEED = {"kt": ("NM", 1852.0), "mph": ("mi", 1609.344), "km/h": ("km", 1000.0), "m/s": ("km", 1000.0)}

FILE_TYPES = [
    ("All flight plans", "*.waypoints *.txt *.mission *.plan *.kml *.kmz"),
    ("Mission Planner waypoints", "*.waypoints *.txt *.mission"),
    ("QGroundControl plan", "*.plan"),
    ("Google Earth KML / KMZ", "*.kml *.kmz"),
    ("All files", "*.*"),
]

# (attribute, label, unit kind, VTOL-only)
AIRCRAFT_FIELDS = [
    ("cruise_airspeed", "Cruise airspeed", "speed", False),
    ("max_airspeed", "Max airspeed", "speed", False),
    ("min_groundspeed", "Min ground speed (0 = off)", "speed", False),
    ("climb_rate", "Climb rate", "vs", False),
    ("sink_rate", "Descent rate", "vs", False),
    ("loiter_radius", "Loiter radius", "alt", False),
    ("rtl_altitude", "RTL altitude (-1 = current)", "alt", False),
    ("vtol_horizontal_speed", "VTOL-mode horizontal speed", "speed", True),
    ("vtol_climb_rate", "VTOL climb rate", "vs", True),
    ("vtol_descent_rate", "VTOL descent rate", "vs", True),
    ("vtol_land_final_alt", "Final landing stage height", "alt", True),
    ("vtol_land_final_speed", "Final landing descent rate", "vs", True),
    ("forward_transition_time", "Forward transition time", "s", True),
    ("back_transition_time", "Back transition time", "s", True),
]

COLOR_SLOWER = "#d1495b"
COLOR_FASTER = "#2e933c"
COLOR_NEUTRAL = "#1f5fbf"

SETTINGS_VERSION = 2

# Sample cell contents used to size table columns from the real font (so they fit at any DPI).
TREE_SAMPLES = {"dist": "999.99", "alt": "999→999", "crs": "000/000", "wind": "100/360",
                "gs0": "999", "gs1": "999", "t0": "0:00:00", "t1": "0:00:00", "dt": "+0:00:00"}


def settings_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "FlightTimeCalc", "settings.json")


def fmt_time(seconds):
    if seconds is None or not math.isfinite(seconds):
        return "not flyable"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s)


def fmt_delta(seconds):
    if seconds is None or not math.isfinite(seconds):
        return "—"
    sign = "+" if seconds >= 0 else "−"
    return sign + fmt_time(abs(seconds))


class TabBar(tk.Frame):
    """A row of large, clearly-highlighted tab buttons above a content area.

    Plain tk widgets are used so the look is identical on Windows, macOS and Linux.
    """

    def __init__(self, parent, font, palette, **kw):
        super().__init__(parent, **kw)
        self.font = font
        self.p = palette
        self.bar = tk.Frame(self)
        self.bar.pack(fill="x")
        self.rule = tk.Frame(self, height=3)
        self.rule.pack(fill="x")
        self.body = tk.Frame(self, highlightthickness=1)
        self.body.pack(fill="both", expand=True)
        self.tabs = []      # (label widget, page)
        self.current = None
        self.restyle(palette)

    def add(self, page, text):
        lbl = tk.Label(self.bar, text=text, font=self.font, padx=16, pady=7, cursor="hand2")
        lbl.pack(side="left", padx=(0, 3))
        idx = len(self.tabs)
        self.tabs.append((lbl, page))
        lbl.bind("<Button-1>", lambda e: self.select(idx))
        lbl.bind("<Enter>", lambda e: idx != self.current and lbl.configure(background=self.p["tab_hover_bg"]))
        lbl.bind("<Leave>", lambda e: idx != self.current and lbl.configure(background=self.p["tab_idle_bg"]))
        if self.current is None:
            self.select(0)
        else:
            self.select(self.current)

    def select(self, idx):
        p = self.p
        for i, (lbl, page) in enumerate(self.tabs):
            if i == idx:
                lbl.configure(background=p["tab_active_bg"], foreground=p["tab_active_fg"])
                page.pack(in_=self.body, fill="both", expand=True)
            else:
                lbl.configure(background=p["tab_idle_bg"], foreground=p["tab_idle_fg"])
                page.pack_forget()
        self.current = idx

    def restyle(self, palette):
        self.p = palette
        for w in (self, self.bar):
            w.configure(background=palette["bg"])
        self.rule.configure(background=palette["tab_active_bg"])
        self.body.configure(background=palette["bg"], highlightbackground=palette["border"],
                            highlightcolor=palette["border"])
        if self.current is not None:
            self.select(self.current)


class App:
    def __init__(self, root, initial_file=None):
        self.root = root
        root.title("%s %s" % (APP_NAME, __version__))
        root.minsize(1000, 640)
        self.fonts = theme.Fonts(root)
        self.scale = theme.ui_scale(root)
        self.appearance = tk.StringVar(value="System")
        self.pal = theme.LIGHT

        self.mission = None
        self.routes = []
        self.file_path = None
        self.forecast = None
        self.layers = []                 # wind layers in SI
        self.calm = None
        self.windy = None
        self.sweep = None
        self._recalc_job = None
        self._queue = queue.Queue()
        self._busy = False

        # Base map state
        self.tiles = basemap.TileLoader()
        self._tile_images = OrderedDict()
        self.map_provider = tk.StringVar(value="OpenStreetMap")
        self.map_zoom = None
        self.map_center = None           # world-pixel centre at map_zoom
        self._map_fitted_for = None
        self._map_redraw_job = None
        self._drag = None

        self.speed_unit = tk.StringVar(value="km/h")
        self.alt_unit = tk.StringVar(value="m")
        self._prev_units = ("km/h", "m")
        self._si_cache = {}
        self.kind = tk.StringVar(value=FIXED_WING)
        self.preset = tk.StringVar(value="Generic fixed wing")
        self.field_vars = {f[0]: tk.StringVar() for f in AIRCRAFT_FIELDS}
        self.include_fw_landing = tk.BooleanVar(value=True)
        self.takeoff_land_as_vtol = tk.BooleanVar(value=False)
        self.rtl_vtol_land = tk.BooleanVar(value=True)
        self.endurance = tk.StringVar(value="0")
        self.wind_mode = tk.StringVar(value="single")
        self.wind_speed = tk.StringVar(value="20")
        self.wind_dir = tk.StringVar(value="270")
        self.kml_alt = tk.StringVar(value="100")
        self.kml_takeoff = tk.BooleanVar(value=True)
        self.kml_rtl = tk.BooleanVar(value=True)
        self.kml_first_is_launch = tk.BooleanVar(value=True)
        self.route_choice = tk.StringVar()
        self.forecast_choice = tk.StringVar()
        self.status = tk.StringVar(value="Open a flight plan to begin.")
        self.wind_status = tk.StringVar(value="Winds can be fetched for the mission area once a flight plan is loaded.")

        self._build_menu()
        self._build_layout()
        self._apply_profile(PRESETS["Generic fixed wing"])
        self._load_settings()
        self.apply_theme()
        self._watch_inputs()
        self._update_kind_visibility()
        self._update_wind_mode()
        self._update_unit_labels()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(200, self._poll_tiles)
        root.after(5000, self._poll_system_theme)
        if initial_file:
            root.after(100, lambda: self.open_file(initial_file))

    # ------------------------------------------------------------------ UI build
    def apply_theme(self):
        """Apply the chosen appearance (light, dark or following the system) to every widget."""
        self.pal = p = theme.resolve(self.appearance.get())
        theme.apply(self.root, p, self.fonts, self._find(ttk.Combobox))
        self.tabbar.restyle(p)
        self.canvas.configure(background=p["map_bg"], highlightbackground=p["border"])
        self.dial.configure(background=p["bg"])
        for menu in self.menus:
            menu.configure(background=p["surface"], foreground=p["fg"], activebackground=p["select_bg"],
                           activeforeground=p["select_fg"], selectcolor=p["fg"])
        self.tree.tag_configure("slower", foreground=p["slower"])
        self.tree.tag_configure("faster", foreground=p["faster"])
        self.tree.tag_configure("bad", background=p["bad_bg"])
        self._size_tree_columns()
        self.draw_dial()
        self.draw_map()

    def _poll_system_theme(self):
        """In 'System' mode, follow the OS if the user switches light/dark while the app is open."""
        if self.appearance.get() == "System" and theme.resolve("System")["name"] != self.pal["name"]:
            self.apply_theme()
        self.root.after(5000, self._poll_system_theme)

    def _find(self, cls, widget=None):
        widget = widget or self.root
        found = [widget] if isinstance(widget, cls) else []
        for child in widget.winfo_children():
            found += self._find(cls, child)
        return found

    def _size_tree_columns(self):
        """Column widths from the actual font so headings and values fit at any display scaling."""
        f = self.fonts.normal
        for col, sample in TREE_SAMPLES.items():
            w = max(f.measure(self.tree_heads[col][0]) + 18, f.measure(sample) + 10)
            self.tree.column(col, width=w, minwidth=w)
        for col in ("seg", "note"):
            w = max(f.measure(self.tree_heads[col][0]) + 18, self._text_widths.get(col, 0))
            if col == "note":
                w = max(w, int(self.tree_heads[col][1] * self.scale))
            self.tree.column(col, width=w, minwidth=w if col == "seg" else 44)
        for col, title, sample in (("alt", "Height (ft)", "10000"), ("spd", "Speed (km/h)", "100.0"),
                                   ("dir", "From °", "360")):
            w = max(f.measure(title) + 22, f.measure(sample) + 16)
            self.layer_tree.column(col, width=w, minwidth=w)

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        fm = tk.Menu(menubar, tearoff=False)
        vm = tk.Menu(menubar, tearoff=False)
        fm.add_command(label="Open flight plan…", command=self.choose_file, accelerator="Ctrl+O")
        fm.add_command(label="Export results to CSV…", command=self.export_csv)
        fm.add_separator()
        fm.add_command(label="Load aircraft profile…", command=self.load_profile)
        fm.add_command(label="Save aircraft profile…", command=self.save_profile)
        fm.add_separator()
        fm.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=fm)
        for name in theme.APPEARANCES:
            vm.add_radiobutton(label="%s appearance" % name, value=name, variable=self.appearance,
                               command=self.apply_theme)
        menubar.add_cascade(label="View", menu=vm)
        hm = tk.Menu(menubar, tearoff=False)
        hm.add_command(label="How the estimate works", command=self.show_help)
        hm.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=hm)
        self.menus = [menubar, fm, vm, hm]
        self.root.config(menu=menubar)
        self.root.bind_all("<Control-o>", lambda e: self.choose_file())
        self.root.bind_all("<Command-o>", lambda e: self.choose_file())

    def _build_layout(self):
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Button(top, text="Open flight plan…", command=self.choose_file).pack(side="left")
        self.file_label = ttk.Label(top, text="No file loaded", style="Caption.TLabel")
        self.file_label.pack(side="left", padx=10)
        self.route_frame = ttk.Frame(top)
        ttk.Label(self.route_frame, text="Path:").pack(side="left")
        self.route_box = ttk.Combobox(self.route_frame, textvariable=self.route_choice, state="readonly", width=40)
        self.route_box.pack(side="left", padx=4)
        self.route_box.bind("<<ComboboxSelected>>", lambda e: self._rebuild_route_mission())
        ttk.Button(top, text="Calculate", command=self.recalculate).pack(side="right")

        bar = ttk.Frame(self.root, padding=(10, 2, 10, 6))
        bar.pack(side="bottom", fill="x")
        ttk.Label(bar, textvariable=self.status, style="Caption.TLabel").pack(side="left")

        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        left = ttk.Frame(paned, width=360)
        paned.add(left, weight=0)
        nb = TabBar(left, self.fonts.tab, self.pal)
        nb.pack(fill="both", expand=True)
        self.tabbar = nb
        nb.add(self._build_wind_tab(nb.body), "Wind")
        nb.add(self._build_aircraft_tab(nb.body), "Aircraft")
        nb.add(self._build_options_tab(nb.body), "Options")

        right_outer = ttk.Frame(paned)
        paned.add(right_outer, weight=1)
        results = ttk.Frame(right_outer, padding=(8, 4))
        results.pack(fill="x")
        self._build_summary(results)
        right = ttk.PanedWindow(right_outer, orient="vertical")
        right.pack(fill="both", expand=True)

        map_pane = ttk.Frame(right)
        right.add(map_pane, weight=3)
        tools = ttk.Frame(map_pane, padding=(0, 0, 0, 4))
        tools.pack(fill="x")
        ttk.Label(tools, text="Base map:").pack(side="left")
        mb = ttk.Combobox(tools, textvariable=self.map_provider, state="readonly", width=22,
                          values=list(basemap.PROVIDERS) + [basemap.NO_MAP])
        mb.pack(side="left", padx=4)
        mb.bind("<<ComboboxSelected>>", lambda e: self._map_provider_changed())
        ttk.Button(tools, text="Fit route", command=self.fit_map).pack(side="right")
        ttk.Button(tools, text="+", width=3, command=lambda: self.zoom_map(1)).pack(side="right", padx=(0, 4))
        ttk.Button(tools, text="−", width=3, command=lambda: self.zoom_map(-1)).pack(side="right", padx=(0, 2))
        ttk.Label(tools, text="Scroll to zoom · drag to pan", style="Caption.TLabel").pack(side="right", padx=8)
        self.canvas = tk.Canvas(map_pane, highlightthickness=1, height=int(260 * self.scale))
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.draw_map())
        self.canvas.bind("<ButtonPress-1>", self._map_press)
        self.canvas.bind("<B1-Motion>", self._map_drag)
        self.canvas.bind("<MouseWheel>", self._map_wheel)                 # Windows / macOS
        self.canvas.bind("<Button-4>", lambda e: self._map_wheel(e, 1))   # Linux
        self.canvas.bind("<Button-5>", lambda e: self._map_wheel(e, -1))

        table = ttk.Frame(right)
        right.add(table, weight=1)
        self._build_table(table)

    def _build_summary(self, parent):
        grid = ttk.Frame(parent)
        grid.pack(fill="x")
        self.sum_vars = {}
        for col, (key, title) in enumerate([("calm", "Zero wind"), ("wind", "With wind"),
                                            ("delta", "Difference"), ("dist", "Distance flown")]):
            box = ttk.Frame(grid, padding=(0, 0, 24, 0))
            box.grid(row=0, column=col, sticky="w")
            ttk.Label(box, text=title, style="Caption.TLabel").pack(anchor="w")
            v = tk.StringVar(value="—")
            self.sum_vars[key] = v
            ttk.Label(box, textvariable=v, style="Big.TLabel").pack(anchor="w")
        self.detail_var = tk.StringVar(value="")
        detail = ttk.Label(parent, textvariable=self.detail_var, justify="left")
        detail.pack(anchor="w", pady=(4, 0))
        self.warn_var = tk.StringVar(value="")
        warn = ttk.Label(parent, textvariable=self.warn_var, style="Warn.TLabel", justify="left")
        warn.pack(anchor="w")
        parent.bind("<Configure>", lambda e: (detail.configure(wraplength=max(e.width - 20, 200)),
                                              warn.configure(wraplength=max(e.width - 20, 200))))

    def _build_table(self, parent):
        cols = ("seg", "dist", "alt", "crs", "wind", "gs0", "gs1", "t0", "t1", "dt", "note")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings", height=8)
        self.tree_heads = {"seg": ("Segment", 200), "dist": ("Dist", 54), "alt": ("Alt", 72),
                           "crs": ("Crs/Hdg", 70), "wind": ("Wind", 64),
                           "gs0": ("GS calm", 70), "gs1": ("GS wind", 70), "t0": ("Time calm", 80),
                           "t1": ("Time wind", 80), "dt": ("Δ", 72), "note": ("Note", 220)}
        for c in cols:
            title, w = self.tree_heads[c]
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, minwidth=w if c == "seg" else 44,
                             anchor="w" if c in ("seg", "note") else "e", stretch=(c == "note"))
        self._text_widths = {}
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(parent, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

    def _build_wind_tab(self, nb):
        f = ttk.Frame(nb, padding=10)

        live = ttk.LabelFrame(f, text="Get current winds for the mission area", padding=8)
        live.pack(fill="x")
        self.btn_meteo = ttk.Button(live, text="Forecast winds aloft (Open-Meteo)",
                                    command=self.fetch_forecast)
        self.btn_meteo.pack(fill="x")
        self.btn_metar = ttk.Button(live, text="Nearest airport wind (NOAA METAR)", command=self.fetch_metar)
        self.btn_metar.pack(fill="x", pady=(4, 0))
        row = ttk.Frame(live)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Forecast hour (UTC):").pack(side="left")
        self.forecast_box = ttk.Combobox(row, textvariable=self.forecast_choice, state="disabled", width=18)
        self.forecast_box.pack(side="left", padx=4)
        self.forecast_box.bind("<<ComboboxSelected>>", lambda e: self._apply_forecast_hour())
        ttk.Label(live, textvariable=self.wind_status, wraplength=int(270 * self.scale), justify="left",
                  style="Caption.TLabel").pack(anchor="w", pady=(6, 0))

        mode = ttk.Frame(f)
        mode.pack(fill="x", pady=(10, 4))
        ttk.Radiobutton(mode, text="One wind for all altitudes", value="single", variable=self.wind_mode,
                        command=self._update_wind_mode).pack(anchor="w")
        ttk.Radiobutton(mode, text="Wind by altitude (layers)", value="layers", variable=self.wind_mode,
                        command=self._update_wind_mode).pack(anchor="w")

        self.single_frame = ttk.LabelFrame(f, text="Wind", padding=8)
        g = ttk.Frame(self.single_frame)
        g.pack(side="left", fill="y")
        ttk.Label(g, text="Speed").grid(row=0, column=0, sticky="w")
        ttk.Entry(g, textvariable=self.wind_speed, width=8).grid(row=0, column=1, padx=4)
        self.wind_speed_unit = ttk.Label(g, text="km/h")
        self.wind_speed_unit.grid(row=0, column=2, sticky="w")
        ttk.Label(g, text="From").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(g, textvariable=self.wind_dir, width=8).grid(row=1, column=1, padx=4, pady=(6, 0))
        ttk.Label(g, text="° true").grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Label(g, text="Click the dial to set\nthe direction the wind\nis coming FROM.",
                  style="Caption.TLabel").grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.dial_size = int(120 * self.scale)
        self.dial = tk.Canvas(self.single_frame, width=self.dial_size, height=self.dial_size, highlightthickness=0)
        self.dial.pack(side="right")
        self.dial.bind("<Button-1>", self._dial_click)
        self.dial.bind("<B1-Motion>", self._dial_click)

        self.layers_frame = ttk.LabelFrame(f, text="Wind layers (height above launch)", padding=8)
        self.layer_tree = ttk.Treeview(self.layers_frame, columns=("alt", "spd", "dir"), show="headings", height=5)
        for c, t in (("alt", "Height"), ("spd", "Speed"), ("dir", "From °")):
            self.layer_tree.heading(c, text=t)
            self.layer_tree.column(c, width=int(90 * self.scale), anchor="e")
        self.layer_tree.pack(fill="x")
        ttk.Button(self.layers_frame, text="Remove selected layer", command=self._remove_layer
                   ).pack(anchor="e", pady=(4, 0))
        add = ttk.LabelFrame(self.layers_frame, text="Add a layer", padding=6)
        add.pack(fill="x", pady=(6, 0))
        self.new_alt, self.new_spd, self.new_dir = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.layer_entry_labels = []
        for col, var in enumerate((self.new_alt, self.new_spd, self.new_dir)):
            lbl = ttk.Label(add, text="", style="Caption.TLabel")
            lbl.grid(row=0, column=col, sticky="w", padx=(0, 4))
            self.layer_entry_labels.append(lbl)
            ttk.Entry(add, textvariable=var, width=7).grid(row=1, column=col, sticky="ew", padx=(0, 4))
            add.columnconfigure(col, weight=1)
        ttk.Button(add, text="Add layer", command=self._add_layer).grid(row=2, column=0, columnspan=3, sticky="e",
                                                                       pady=(6, 0))
        return f

    def _build_aircraft_tab(self, nb):
        outer = ttk.Frame(nb, padding=10)
        row = ttk.Frame(outer)
        row.pack(fill="x")
        ttk.Label(row, text="Preset").pack(side="left")
        box = ttk.Combobox(row, textvariable=self.preset, values=list(PRESETS), state="readonly", width=26)
        box.pack(side="left", padx=4)
        box.bind("<<ComboboxSelected>>", lambda e: self._apply_profile(PRESETS[self.preset.get()]))
        row2 = ttk.Frame(outer)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Button(row2, text="Load…", command=self.load_profile).pack(side="left")
        ttk.Button(row2, text="Save…", command=self.save_profile).pack(side="left", padx=4)

        kind = ttk.Frame(outer)
        kind.pack(fill="x", pady=(8, 4))
        ttk.Radiobutton(kind, text="Fixed wing", value=FIXED_WING, variable=self.kind,
                        command=self._update_kind_visibility).pack(side="left")
        ttk.Radiobutton(kind, text="VTOL (QuadPlane)", value=VTOL, variable=self.kind,
                        command=self._update_kind_visibility).pack(side="left", padx=10)

        self.fw_frame = ttk.LabelFrame(outer, text="Forward flight", padding=8)
        self.fw_frame.pack(fill="x")
        self.vtol_frame = ttk.LabelFrame(outer, text="VTOL", padding=8)
        self.unit_labels = {}
        rows = {False: 0, True: 0}
        for attr, label, kind_, vtol_only in AIRCRAFT_FIELDS:
            parent = self.vtol_frame if vtol_only else self.fw_frame
            r = rows[vtol_only]
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=1)
            ttk.Entry(parent, textvariable=self.field_vars[attr], width=8).grid(row=r, column=1, padx=4)
            ul = ttk.Label(parent, text="", width=7)
            ul.grid(row=r, column=2, sticky="w")
            self.unit_labels[attr] = (ul, kind_)
            rows[vtol_only] += 1
        ttk.Checkbutton(self.fw_frame, text="Include landing descent after RTL",
                        variable=self.include_fw_landing).grid(row=rows[False], column=0, columnspan=3, sticky="w")
        r = rows[True]
        ttk.Checkbutton(self.vtol_frame, text="Treat TAKEOFF / LAND as VTOL", variable=self.takeoff_land_as_vtol
                        ).grid(row=r, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(self.vtol_frame, text="RTL ends with VTOL landing", variable=self.rtl_vtol_land
                        ).grid(row=r + 1, column=0, columnspan=3, sticky="w")

        self.endurance_frame = ttk.Frame(outer)
        self.endurance_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(self.endurance_frame, text="Max endurance (0 = don't check)").pack(side="left")
        ttk.Entry(self.endurance_frame, textvariable=self.endurance, width=6).pack(side="left", padx=4)
        ttk.Label(self.endurance_frame, text="min").pack(side="left")
        return outer

    def _build_options_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        look = ttk.LabelFrame(f, text="Appearance", padding=8)
        look.pack(fill="x", pady=(0, 10))
        for name, desc in (("System", "System (follow the computer's setting)"), ("Light", "Light"),
                           ("Dark", "Dark")):
            ttk.Radiobutton(look, text=desc, value=name, variable=self.appearance,
                            command=self.apply_theme).pack(anchor="w")
        units = ttk.LabelFrame(f, text="Units", padding=8)
        units.pack(fill="x")
        ttk.Label(units, text="Speed").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(units, textvariable=self.speed_unit, values=list(SPEED_UNITS), state="readonly", width=8)
        cb.grid(row=0, column=1, padx=4, pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._units_changed())
        ttk.Label(units, text="Altitude").grid(row=1, column=0, sticky="w")
        cb = ttk.Combobox(units, textvariable=self.alt_unit, values=list(ALT_UNITS), state="readonly", width=8)
        cb.grid(row=1, column=1, padx=4, pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._units_changed())

        kml = ttk.LabelFrame(f, text="KML / KMZ paths", padding=8)
        kml.pack(fill="x", pady=(10, 0))
        ttk.Label(kml, text="Altitude when the path has none").grid(row=0, column=0, sticky="w")
        ttk.Entry(kml, textvariable=self.kml_alt, width=7).grid(row=0, column=1, padx=4)
        self.kml_alt_unit = ttk.Label(kml, text="m")
        self.kml_alt_unit.grid(row=0, column=2, sticky="w")
        for r, (text, var) in enumerate([("First point is the launch location", self.kml_first_is_launch),
                                         ("Add take-off at start", self.kml_takeoff),
                                         ("Add return-to-launch at end", self.kml_rtl)], start=1):
            ttk.Checkbutton(kml, text=text, variable=var, command=self._rebuild_route_mission
                            ).grid(row=r, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Mission Planner (.waypoints) and QGroundControl (.plan) files are used exactly as "
                          "written: take-off, loiters, DO_JUMP loops, speed changes, VTOL transitions and "
                          "landing are all simulated.", wraplength=int(270 * self.scale), style="Caption.TLabel",
                  justify="left").pack(anchor="w", pady=(10, 0))
        return f

    # ------------------------------------------------------------------ units
    def _speed_factor(self):
        return SPEED_UNITS[self.speed_unit.get()]

    def _alt_factor(self):
        return ALT_UNITS[self.alt_unit.get()]

    def _unit_info(self, kind, units=None):
        su, au = units or (self.speed_unit.get(), self.alt_unit.get())
        if kind == "speed":
            return su, SPEED_UNITS[su]
        if kind == "alt":
            return au, ALT_UNITS[au]
        if kind == "vs":
            return ("m/s", 1.0) if au == "m" else ("ft/min", 0.3048 / 60.0)
        return "s", 1.0

    def _units_changed(self):
        old = self._prev_units
        new = (self.speed_unit.get(), self.alt_unit.get())
        if old == new:
            return
        for attr, _, kind, _ in AIRCRAFT_FIELDS:
            self._convert_var(self.field_vars[attr], kind, old, new)
        self._convert_var(self.wind_speed, "speed", old, new)
        self._convert_var(self.kml_alt, "alt", old, new)
        self._prev_units = new
        self._update_unit_labels()
        self._refresh_layer_tree()
        self.schedule_recalc()

    def _convert_var(self, var, kind, old, new):
        text = var.get()
        try:
            v = float(text)
        except ValueError:
            return
        if kind == "alt" and v < 0:
            return  # sentinel such as -1
        # Re-use the exact SI value we last wrote so repeated unit switches don't accumulate rounding.
        cached = self._si_cache.get(str(var))
        si = cached[1] if cached and cached[0] == text else v * self._unit_info(kind, old)[1]
        self._set_si(var, kind, si)

    def _set_si(self, var, kind, si):
        text = self._num(si / self._unit_info(kind)[1])
        self._si_cache[str(var)] = (text, si)
        var.set(text)

    @staticmethod
    def _num(v):
        if abs(v - round(v)) < 1e-6:
            return str(int(round(v)))
        return ("%.2f" % v).rstrip("0").rstrip(".")

    def _update_unit_labels(self):
        for attr, (label, kind) in self.unit_labels.items():
            label.configure(text=self._unit_info(kind)[0])
        self.wind_speed_unit.configure(text=self.speed_unit.get())
        self.kml_alt_unit.configure(text=self.alt_unit.get())
        self.layer_tree.heading("alt", text="Height (%s)" % self.alt_unit.get())
        self.layer_tree.heading("spd", text="Speed (%s)" % self.speed_unit.get())
        for lbl, txt in zip(self.layer_entry_labels, ("Height (%s)" % self.alt_unit.get(),
                                                      "Speed (%s)" % self.speed_unit.get(), "From (°)")):
            lbl.configure(text=txt)

    # ------------------------------------------------------------------ aircraft
    def _apply_profile(self, p: AircraftProfile):
        for attr, _, kind, _ in AIRCRAFT_FIELDS:
            v = getattr(p, attr)
            if kind == "alt" and v < 0:
                self.field_vars[attr].set(self._num(v))
            else:
                self._set_si(self.field_vars[attr], kind, v)
        self.kind.set(p.kind)
        self.include_fw_landing.set(p.include_fw_landing)
        self.takeoff_land_as_vtol.set(p.takeoff_land_as_vtol)
        self.rtl_vtol_land.set(p.rtl_vtol_land)
        self.endurance.set(self._num(p.endurance_min))
        self._update_kind_visibility()
        self.schedule_recalc()

    def read_profile(self):
        p = AircraftProfile(name=self.preset.get(), kind=self.kind.get())
        for attr, label, kind, _ in AIRCRAFT_FIELDS:
            try:
                v = float(self.field_vars[attr].get())
            except ValueError:
                raise ValueError("'%s' must be a number." % label)
            if kind == "alt" and v < 0:
                setattr(p, attr, -1.0)
            else:
                if v < 0:
                    raise ValueError("'%s' cannot be negative." % label)
                setattr(p, attr, v * self._unit_info(kind)[1])
        for attr in ("cruise_airspeed", "climb_rate", "sink_rate"):
            if getattr(p, attr) <= 0:
                raise ValueError("Cruise airspeed, climb rate and descent rate must be greater than zero.")
        if p.kind == VTOL and (p.vtol_climb_rate <= 0 or p.vtol_descent_rate <= 0 or p.vtol_horizontal_speed <= 0):
            raise ValueError("VTOL speeds and climb/descent rates must be greater than zero.")
        p.max_airspeed = max(p.max_airspeed, p.cruise_airspeed)
        p.include_fw_landing = self.include_fw_landing.get()
        p.takeoff_land_as_vtol = self.takeoff_land_as_vtol.get()
        p.rtl_vtol_land = self.rtl_vtol_land.get()
        try:
            p.endurance_min = max(float(self.endurance.get() or 0), 0.0)
        except ValueError:
            raise ValueError("Endurance must be a number of minutes.")
        return p

    def _update_kind_visibility(self):
        if self.kind.get() == VTOL:
            self.vtol_frame.pack(fill="x", pady=(8, 0), before=self.endurance_frame)
        else:
            self.vtol_frame.pack_forget()
        if self.routes:
            self._rebuild_route_mission(recalc=False)
        self.schedule_recalc()

    def save_profile(self):
        try:
            p = self.read_profile()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        path = filedialog.asksaveasfilename(title="Save aircraft profile", defaultextension=".json",
                                            filetypes=[("Aircraft profile", "*.json")])
        if path:
            p.name = os.path.splitext(os.path.basename(path))[0]
            with open(path, "w", encoding="utf-8") as f:
                f.write(p.to_json())
            self.status.set("Saved aircraft profile to %s" % path)

    def load_profile(self):
        path = filedialog.askopenfilename(title="Load aircraft profile", filetypes=[("Aircraft profile", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                p = AircraftProfile.from_json(f.read())
        except Exception as e:
            messagebox.showerror(APP_NAME, "Could not read the profile:\n%s" % e)
            return
        self.preset.set(p.name)
        self._apply_profile(p)

    # ------------------------------------------------------------------ wind
    def _update_wind_mode(self):
        if self.wind_mode.get() == "single":
            self.layers_frame.pack_forget()
            self.single_frame.pack(fill="x")
        else:
            self.single_frame.pack_forget()
            self.layers_frame.pack(fill="x")
        self.draw_dial()
        self.schedule_recalc()

    def draw_dial(self):
        c = self.dial
        c.delete("all")
        p = self.pal
        cx = cy = self.dial_size / 2
        r = self.dial_size * 0.38
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=p["dial_ring"], width=2, fill=p["dial_face"])
        for ang, txt in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
            a = math.radians(ang)
            c.create_text(cx + (r + 9) * math.sin(a), cy - (r + 9) * math.cos(a), text=txt,
                          font=self.fonts.small_bold, fill=p["fg"])
        for ang in range(0, 360, 30):
            a = math.radians(ang)
            c.create_line(cx + (r - 5) * math.sin(a), cy - (r - 5) * math.cos(a),
                          cx + r * math.sin(a), cy - r * math.cos(a), fill=p["dial_tick"])
        try:
            d = float(self.wind_dir.get())
        except ValueError:
            return
        a = math.radians(d)
        # Arrow starts at the "from" side and points the way the wind blows.
        x0, y0 = cx + (r - 6) * math.sin(a), cy - (r - 6) * math.cos(a)
        x1, y1 = cx - (r - 14) * math.sin(a), cy + (r - 14) * math.cos(a)
        c.create_line(x0, y0, x1, y1, arrow="last", width=3, fill=COLOR_NEUTRAL, arrowshape=(12, 14, 5))

    def _dial_click(self, event):
        half = self.dial_size / 2
        d = math.degrees(math.atan2(event.x - half, half - event.y)) % 360
        self.wind_dir.set(str(int(round(d / 5.0) * 5) % 360))

    def _refresh_layer_tree(self):
        self.layer_tree.delete(*self.layer_tree.get_children())
        sf, af = self._speed_factor(), self._alt_factor()
        for i, l in enumerate(sorted(self.layers, key=lambda l: l.alt_agl_m)):
            self.layer_tree.insert("", "end", iid=str(i), values=(
                self._num(round(l.alt_agl_m / af)), "%.1f" % (l.speed_ms / sf), "%03d" % round(l.direction_from_deg)))
        self.layers.sort(key=lambda l: l.alt_agl_m)

    def _add_layer(self):
        try:
            alt = float(self.new_alt.get()) * self._alt_factor()
            spd = float(self.new_spd.get()) * self._speed_factor()
            d = float(self.new_dir.get()) % 360
        except ValueError:
            messagebox.showerror(APP_NAME, "Enter a height, a speed and a from-direction for the new layer.")
            return
        self.layers = [l for l in self.layers if abs(l.alt_agl_m - alt) > 0.5] + [WindLayer(alt, spd, d)]
        self.new_alt.set("")
        self.new_spd.set("")
        self.new_dir.set("")
        self._refresh_layer_tree()
        self.schedule_recalc()

    def _remove_layer(self):
        sel = [int(i) for i in self.layer_tree.selection()]
        self.layers = [l for i, l in enumerate(self.layers) if i not in sel]
        self._refresh_layer_tree()
        self.schedule_recalc()

    def read_wind(self):
        if self.wind_mode.get() == "layers":
            if not self.layers:
                raise ValueError("Add at least one wind layer, or fetch winds, or switch to a single wind.")
            src = self.forecast.source if self.forecast else "Manual layers"
            return WindProfile(list(self.layers), source=src)
        try:
            spd = float(self.wind_speed.get() or 0) * self._speed_factor()
            d = float(self.wind_dir.get() or 0) % 360
        except ValueError:
            raise ValueError("Wind speed and direction must be numbers.")
        if spd < 0:
            raise ValueError("Wind speed cannot be negative.")
        return WindProfile.uniform(spd, d)

    def _require_location(self):
        if not self.mission:
            messagebox.showinfo(APP_NAME, "Load a flight plan first so winds can be fetched for its location.")
            return None
        return self.mission.centroid()

    def fetch_forecast(self):
        loc = self._require_location()
        if loc:
            self._run_background("Fetching Open-Meteo forecast…", lambda: fetch_open_meteo(*loc),
                                 self._got_forecast)

    def fetch_metar(self):
        loc = self._require_location()
        if loc:
            self._run_background("Fetching nearest METAR…", lambda: fetch_nearest_metar(*loc), self._got_metar)

    def _run_background(self, msg, work, done):
        if self._busy:
            return
        self._busy = True
        self.wind_status.set(msg)
        self.btn_meteo.state(["disabled"])
        self.btn_metar.state(["disabled"])

        def worker():
            try:
                self._queue.put((done, work(), None))
            except Exception as e:
                self._queue.put((done, None, e))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self._poll_queue)

    def _poll_queue(self):
        try:
            done, result, err = self._queue.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_queue)
            return
        self._busy = False
        self.btn_meteo.state(["!disabled"])
        self.btn_metar.state(["!disabled"])
        if err is not None:
            msg = str(err) if isinstance(err, WeatherError) else "Unexpected error: %s" % err
            self.wind_status.set(msg)
            hint = ("You can enter the wind manually in the meantime." if "refused" in msg else
                    "Check your internet connection, or enter the wind manually.")
            messagebox.showerror(APP_NAME, msg + "\n\n" + hint)
            return
        done(result)

    def _got_forecast(self, fc):
        self.forecast = fc
        labels = [t.strftime("%a %d %b %H:%M") for t in fc.times]
        self.forecast_box.configure(values=labels, state="readonly")
        idx = fc.nearest_index()
        self.forecast_choice.set(labels[idx])
        self._apply_forecast_hour()

    def _apply_forecast_hour(self):
        fc = self.forecast
        if not fc:
            return
        labels = list(self.forecast_box.cget("values"))
        try:
            idx = labels.index(self.forecast_choice.get())
        except ValueError:
            return
        prof = fc.profiles[idx]
        self.layers = [WindLayer(l.alt_agl_m, l.speed_ms, l.direction_from_deg) for l in prof.layers]
        self._refresh_layer_tree()
        # Also fill the single-wind fields with the wind at a typical mission height.
        ref = self._typical_alt()
        s, d = prof.at(ref)
        self.wind_speed.set("%.1f" % (s / self._speed_factor()))
        self.wind_dir.set(str(int(round(d)) % 360))
        self.wind_mode.set("layers")
        self._update_wind_mode()
        self.wind_status.set("Open-Meteo forecast for %.3f, %.3f (ground %.0f m MSL), %s UTC. %d layers up to %s %s. "
                             "At %s %s: %.0f %s from %03d°." % (
                                 fc.lat, fc.lon, fc.elevation_m, self.forecast_choice.get(), len(prof.layers),
                                 self._num(round(max(l.alt_agl_m for l in prof.layers) / self._alt_factor())),
                                 self.alt_unit.get(), self._num(round(ref / self._alt_factor())), self.alt_unit.get(),
                                 s / self._speed_factor(), self.speed_unit.get(), round(d) % 360))

    def _got_metar(self, w):
        self.wind_mode.set("single")
        self.wind_speed.set("%.1f" % (w.speed_ms / self._speed_factor()))
        self.wind_dir.set(str(int(round(w.direction_from_deg)) % 360))
        self._update_wind_mode()
        gust = ", gusting %.0f %s" % (w.gust_ms / self._speed_factor(), self.speed_unit.get()) if w.gust_ms else ""
        var = " Direction is VARIABLE; enter one or check the worst case below." if w.variable else ""
        self.wind_status.set("METAR %s %s, %.0f km away, observed %s: %.0f %s%s. Surface wind is usually lighter "
                             "than wind at mission height.%s" % (
                                 w.station, w.name, w.distance_km, w.observed, w.speed_ms / self._speed_factor(),
                                 self.speed_unit.get(), gust, var))

    def _typical_alt(self):
        if not self.mission:
            return 100.0
        alts = sorted(i.alt for i in self.mission.items if i.alt > 0)
        return alts[len(alts) // 2] if alts else 100.0

    # ------------------------------------------------------------------ files
    def choose_file(self):
        path = filedialog.askopenfilename(title="Open flight plan", filetypes=FILE_TYPES)
        if path:
            self.open_file(path)

    def open_file(self, path):
        try:
            kind, data = M.load_file(path)
        except (M.MissionParseError, OSError) as e:
            messagebox.showerror(APP_NAME, "Could not load %s:\n\n%s" % (os.path.basename(path), e))
            return
        except Exception as e:
            messagebox.showerror(APP_NAME, "Unexpected error reading %s:\n\n%s" % (os.path.basename(path), e))
            return
        self.file_path = path
        self.file_label.configure(text=os.path.basename(path))
        old_center = self.mission.centroid() if self.mission else None
        if kind == "routes":
            self.routes = data
            names = ["%d. %s (%d pts)" % (i + 1, r.name, len(r.points)) for i, r in enumerate(data)]
            longest = max(range(len(data)), key=lambda i: len(data[i].points))
            self.route_box.configure(values=names)
            self.route_choice.set(names[longest])
            if len(data) > 1:
                self.route_frame.pack(side="left", padx=10)
            else:
                self.route_frame.pack_forget()
            self._rebuild_route_mission(recalc=False)
        else:
            self.routes = []
            self.route_frame.pack_forget()
            self.mission = data
            vtol_cmds = {M.NAV_VTOL_TAKEOFF, M.NAV_VTOL_LAND, M.DO_VTOL_TRANSITION}
            if self.kind.get() != VTOL and any(i.command in vtol_cmds for i in data.items):
                self.kind.set(VTOL)
                self._update_kind_visibility()
                messagebox.showinfo(APP_NAME, "This mission contains VTOL commands, so the aircraft type was "
                                              "switched to VTOL (QuadPlane). Check the VTOL settings on the "
                                              "Aircraft tab.")
        if self.forecast and old_center:
            c = self.mission.centroid()
            if abs(c[0] - old_center[0]) > 0.3 or abs(c[1] - old_center[1]) > 0.3:
                self.forecast = None
                self.forecast_box.configure(values=[], state="disabled")
                self.forecast_choice.set("")
                self.wind_status.set("New mission area - fetch winds again for this location.")
        self.recalculate()

    def _rebuild_route_mission(self, recalc=True):
        if not self.routes:
            return
        try:
            idx = list(self.route_box.cget("values")).index(self.route_choice.get())
        except ValueError:
            idx = 0
        try:
            alt = float(self.kml_alt.get()) * self._alt_factor()
        except ValueError:
            alt = 100.0
        name = os.path.splitext(os.path.basename(self.file_path or "route"))[0]
        self.mission = M.route_to_mission(self.routes[idx], default_alt_m=alt, add_takeoff=self.kml_takeoff.get(),
                                          add_rtl=self.kml_rtl.get(), vtol=self.kind.get() == VTOL,
                                          first_point_is_launch=self.kml_first_is_launch.get(), name=name)
        if recalc:
            self.schedule_recalc()

    # ------------------------------------------------------------------ calculation
    def _watch_inputs(self):
        for v in list(self.field_vars.values()) + [self.wind_speed, self.endurance, self.include_fw_landing,
                                                   self.takeoff_land_as_vtol, self.rtl_vtol_land]:
            v.trace_add("write", lambda *a: self.schedule_recalc())
        self.wind_dir.trace_add("write", lambda *a: (self.draw_dial(), self.schedule_recalc()))
        self.kml_alt.trace_add("write", lambda *a: self._rebuild_route_mission())

    def schedule_recalc(self):
        if self._recalc_job:
            self.root.after_cancel(self._recalc_job)
        self._recalc_job = self.root.after(350, self.recalculate)

    def recalculate(self):
        self._recalc_job = None
        if not self.mission:
            return
        try:
            ac = self.read_profile()
            wind = self.read_wind()
        except ValueError as e:
            self.status.set("⚠ " + str(e))
            return
        self.calm = estimate(self.mission, ac)
        self.windy = estimate(self.mission, ac, wind)
        self.sweep = None if wind.is_calm() else direction_sweep(self.mission, ac, wind, 10)
        self._show_results(ac, wind)
        self.draw_map()

    def _show_results(self, ac, wind):
        t0, t1 = self.calm.total_time_s, self.windy.total_time_s
        self.sum_vars["calm"].set(fmt_time(t0))
        self.sum_vars["wind"].set(fmt_time(t1))
        if math.isfinite(t0) and math.isfinite(t1) and t0 > 0:
            self.sum_vars["delta"].set("%s (%+.0f%%)" % (fmt_delta(t1 - t0), 100 * (t1 - t0) / t0))
        else:
            self.sum_vars["delta"].set("—")
        dname, dfac = DIST_FOR_SPEED[self.speed_unit.get()]
        self.sum_vars["dist"].set("%.1f %s" % (self.calm.total_distance_m / dfac, dname))

        details = []
        m = self.mission
        details.append("%s · %d items · home %.5f, %.5f · %s" % (
            m.source_format, len(m.items), m.home_lat, m.home_lon, wind.source if not wind.is_calm() else "no wind"))
        if self.sweep:
            finite = [(o, t) for o, t in self.sweep if math.isfinite(t)]
            base_dir = wind.at(self._typical_alt())[1]
            if finite:
                worst = max(finite, key=lambda x: x[1])
                best = min(finite, key=lambda x: x[1])
                details.append("Same wind speed from any direction: best %s (from %03d°), worst %s (from %03d°)%s." % (
                    fmt_time(best[1]), (base_dir + best[0]) % 360, fmt_time(worst[1]), (base_dir + worst[0]) % 360,
                    "" if len(finite) == len(self.sweep) else "; some directions are not flyable"))
            else:
                details.append("This wind speed is too strong for the aircraft from every direction.")
        if ac.endurance_min > 0 and math.isfinite(t1):
            reserve = ac.endurance_min * 60 - t1
            pct = 100 * reserve / (ac.endurance_min * 60)
            if reserve < 0:
                details.append("⚠ Flight time with wind EXCEEDS the %.0f min endurance by %s." % (
                    ac.endurance_min, fmt_time(-reserve)))
            else:
                details.append("Endurance reserve with wind: %s (%.0f%%)%s" % (
                    fmt_time(reserve), pct, "  ⚠ below 20%" if pct < 20 else ""))
        self.detail_var.set("\n".join(details))
        warns = list(dict.fromkeys(m.warnings + self.windy.warnings + self.calm.warnings))
        self.warn_var.set("\n".join("⚠ " + w for w in warns[:6]))

        self.tree.delete(*self.tree.get_children())
        sf, af = self._speed_factor(), self._alt_factor()
        tree_font = self.fonts.normal
        widest = {"seg": 0, "note": 0}
        for i, (a, b) in enumerate(zip(self.calm.segments, self.windy.segments)):
            alt = (self._num(round(b.alt_end / af)) if abs(b.alt_end - b.alt_start) < 0.5 else
                   "%s→%s" % (self._num(round(b.alt_start / af)), self._num(round(b.alt_end / af))))
            wind_txt = "%.0f/%03d" % (b.wind_speed / sf, round(b.wind_from) % 360) if b.wind_speed > 0.05 else ""
            dt = b.time_s - a.time_s
            tags = ()
            if not math.isfinite(b.time_s):
                tags = ("bad",)
            elif dt > 0.5:
                tags = ("slower",)
            elif dt < -0.5:
                tags = ("faster",)
            label = b.label.replace("_", " ")
            widest["seg"] = max(widest["seg"], tree_font.measure(label))
            widest["note"] = max(widest["note"], tree_font.measure(b.note))
            self.tree.insert("", "end", values=(
                label,
                "%.2f" % (b.distance_m / dfac) if b.distance_m else "",
                alt,
                ("%03d/%03d" % (round(b.course_deg) % 360, round(b.heading_deg) % 360)
                 if b.course_deg is not None and b.heading_deg is not None else
                 ("%03d" % round(b.course_deg) if b.course_deg is not None else "")),
                wind_txt,
                "%.0f" % (a.groundspeed / sf) if a.groundspeed else "",
                "%.0f" % (b.groundspeed / sf) if b.groundspeed else ("—" if b.kind == "leg" else ""),
                fmt_time(a.time_s), fmt_time(b.time_s), fmt_delta(dt) if abs(dt) >= 0.5 else "",
                b.note), tags=tags)
        # Size the text columns to their longest entry so nothing is cut off.
        self._text_widths = {"seg": widest["seg"] + 12, "note": widest["note"] + 12}
        self._size_tree_columns()
        self.status.set("Calculated %d segments. Wind column: %s / from °. Speeds in %s, heights in %s above launch."
                        % (len(self.windy.segments), self.speed_unit.get(), self.speed_unit.get(),
                           self.alt_unit.get()))

    # ------------------------------------------------------------------ map
    def _provider(self):
        name = self.map_provider.get()
        return name if name in basemap.PROVIDERS else None

    def _max_zoom(self):
        p = self._provider()
        return basemap.PROVIDERS[p]["max_zoom"] if p else 19

    def _map_bounds(self):
        m = self.mission
        lats = [p[0] for p in m.positions()]
        lons = [p[1] for p in m.positions()]
        for s in (self.windy.segments if self.windy else []):
            if s.loiter_radius and s.start:
                dlat = s.loiter_radius / 111195.0
                dlon = dlat / max(math.cos(math.radians(s.start[0])), 0.01)
                lats += [s.start[0] - dlat, s.start[0] + dlat]
                lons += [s.start[1] - dlon, s.start[1] + dlon]
        return min(lats), min(lons), max(lats), max(lons)

    def fit_map(self, redraw=True):
        if not self.mission:
            return
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 50 or h < 50:
            return
        b = self._map_bounds()
        z = basemap.fit_zoom(b, w, h, margin=60, max_zoom=min(self._max_zoom(), 18))
        x0, y0 = basemap.world_px(b[2], b[1], z)
        x1, y1 = basemap.world_px(b[0], b[3], z)
        self.map_zoom, self.map_center = z, ((x0 + x1) / 2, (y0 + y1) / 2)
        self._map_fitted_for = id(self.mission)
        if redraw:
            self.draw_map()

    def zoom_map(self, dz, sx=None, sy=None):
        if self.map_zoom is None:
            return
        nz = max(2, min(self._max_zoom(), self.map_zoom + dz))
        if nz == self.map_zoom:
            return
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        sx = w / 2 if sx is None else sx
        sy = h / 2 if sy is None else sy
        f = 2 ** (nz - self.map_zoom)
        cx, cy = self.map_center
        # Keep the point under the cursor fixed while zooming.
        wx, wy = cx + (sx - w / 2), cy + (sy - h / 2)
        self.map_center = (wx * f - (sx - w / 2), wy * f - (sy - h / 2))
        self.map_zoom = nz
        self.tiles.cancel_all()
        self.draw_map()

    def _map_wheel(self, event, direction=None):
        if direction is None:
            direction = 1 if event.delta > 0 else -1
        self.zoom_map(direction, event.x, event.y)

    def _map_press(self, event):
        self._drag = (event.x, event.y, self.map_center)

    def _map_drag(self, event):
        if not self._drag or self.map_center is None:
            return
        x0, y0, (cx, cy) = self._drag
        self.map_center = (cx - (event.x - x0), cy - (event.y - y0))
        self.draw_map()

    def _map_provider_changed(self):
        self.tiles.cancel_all()
        if self.map_zoom is not None and self.map_zoom > self._max_zoom():
            self.zoom_map(self._max_zoom() - self.map_zoom)
        else:
            self.draw_map()

    def _poll_tiles(self):
        got = False
        try:
            while True:
                key = self.tiles.ready.get_nowait()
                if key[0] == self._provider() and key[1] == self.map_zoom:
                    got = True
        except queue.Empty:
            pass
        if got and self._map_redraw_job is None:
            self._map_redraw_job = self.root.after(60, self._redraw_from_tiles)
        self.root.after(150, self._poll_tiles)

    def _redraw_from_tiles(self):
        self._map_redraw_job = None
        self.draw_map()

    def _tile_image(self, provider, z, x, y):
        key = (provider, z, x, y)
        img = self._tile_images.get(key)
        if img is not None:
            self._tile_images.move_to_end(key)
            return img
        path = self.tiles.cached(provider, z, x, y)
        if path is None:
            self.tiles.request(provider, z, x, y)
            path = self.tiles.stale(provider, z, x, y)   # show an old copy while refreshing
            if path is None:
                return None
        try:
            img = tk.PhotoImage(file=path)
        except tk.TclError:
            return None
        self._tile_images[key] = img
        while len(self._tile_images) > 300:
            self._tile_images.popitem(last=False)
        return img

    def _label(self, x, y, text, anchor="nw", font=None, pad=3):
        """Text on a solid box so it stays readable over the map."""
        c, p = self.canvas, self.pal
        t = c.create_text(x, y, text=text, anchor=anchor, font=font or self.fonts.small, fill=p["overlay_fg"])
        x0, y0, x1, y1 = c.bbox(t)
        r = c.create_rectangle(x0 - pad, y0 - pad, x1 + pad, y1 + pad, fill=p["overlay_bg"],
                               outline=p["overlay_border"])
        c.tag_lower(r, t)
        return x0 - pad, y0 - pad, x1 + pad, y1 + pad

    def _box(self, x0, y0, x1, y1):
        p = self.pal
        return self.canvas.create_rectangle(x0, y0, x1, y1, fill=p["overlay_bg"], outline=p["overlay_border"])

    def draw_map(self):
        c, p, F = self.canvas, self.pal, self.fonts
        k = self.scale
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        if not self.mission or not self.windy:
            c.create_text(w / 2, h / 2, text="Open a Mission Planner (.waypoints), QGroundControl (.plan),\n"
                                             "KML or KMZ flight plan to see the route here.",
                          fill=p["muted"], justify="center", font=F.normal)
            return
        if self._map_fitted_for != id(self.mission) or self.map_zoom is None:
            self.fit_map(redraw=False)
        m, z = self.mission, self.map_zoom
        cx, cy = self.map_center
        ox, oy = cx - w / 2, cy - h / 2

        # --- base map tiles
        provider = self._provider()
        if provider:
            n = 2 ** z
            for ty in range(int(oy // 256), int((oy + h) // 256) + 1):
                if ty < 0 or ty >= n:
                    continue
                for tx in range(int(ox // 256), int((ox + w) // 256) + 1):
                    img = self._tile_image(provider, z, tx % n, ty)
                    if img is not None:
                        c.create_image(tx * 256 - ox, ty * 256 - oy, image=img, anchor="nw")

        def scr(lat, lon):
            x, y = basemap.world_px(lat, lon, z)
            return x - ox, y - oy

        mpp = basemap.metres_per_px(m.home_lat, z)
        casing = "#ffffff" if provider else p["map_bg"]

        # --- route (a casing under each line keeps it visible on any map)
        for a, b in zip(self.calm.segments, self.windy.segments):
            if b.start is None or b.end is None:
                continue
            if b.loiter_radius:
                x, y = scr(*b.start)
                r = b.loiter_radius / mpp
                c.create_oval(x - r, y - r, x + r, y + r, outline=casing, width=5)
                c.create_oval(x - r, y - r, x + r, y + r, outline="#9b59d0", dash=(6, 4), width=3)
                continue
            if b.distance_m < 0.5:
                continue
            ratio = b.time_s / a.time_s if a.time_s > 0 else 1.0
            color = COLOR_SLOWER if ratio > 1.03 else (COLOR_FASTER if ratio < 0.97 else COLOR_NEUTRAL)
            if not math.isfinite(b.time_s):
                color = p["fg"]
            x0, y0 = scr(*b.start)
            x1, y1 = scr(*b.end)
            c.create_line(x0, y0, x1, y1, fill=casing, width=8 * k, capstyle="round")
            c.create_line(x0, y0, x1, y1, fill=color, width=4 * k, arrow="last",
                          arrowshape=(13 * k, 16 * k, 6 * k))

        dot = 5 * k
        for it in m.items:
            if it.has_position and it.command not in (M.DO_JUMP, M.DO_CHANGE_SPEED):
                x, y = scr(it.lat, it.lon)
                c.create_oval(x - dot, y - dot, x + dot, y + dot, fill=p["overlay_bg"], outline=p["overlay_fg"],
                              width=2)
                self._label(x + dot + 3, y - dot - 3, str(it.seq), anchor="sw", font=F.small_bold, pad=1)
        hx, hy = scr(m.home_lat, m.home_lon)
        c.create_polygon(hx, hy - 11 * k, hx - 9 * k, hy + 7 * k, hx + 9 * k, hy + 7 * k, fill="#f2b134",
                         outline="#5a3f00", width=2)
        self._label(hx, hy + 10 * k, "Launch", anchor="n", font=F.small_bold, pad=1)

        # --- overlays: legend, north, wind, scale, attribution (sized from the font height)
        line = F.line(F.small)
        lx, ly, lh = 10, 10, line + 8
        box = self._box(lx, ly, lx, ly + lh)
        x = lx + 6
        c.create_text(x, ly + lh / 2, text="Legs:", anchor="w", font=F.small_bold, fill=p["overlay_fg"])
        x += F.small_bold.measure("Legs:") + 12
        for col, txt in ((COLOR_SLOWER, "slower with wind"), (COLOR_FASTER, "faster"),
                         (COLOR_NEUTRAL, "little change")):
            c.create_line(x, ly + lh / 2, x + 18 * k, ly + lh / 2, fill=col, width=4 * k)
            x += 18 * k + 5
            c.create_text(x, ly + lh / 2, text=txt, anchor="w", font=F.small, fill=p["overlay_fg"])
            x += F.small.measure(txt) + 14
        c.coords(box, lx, ly, x - 6, ly + lh)

        nr = 15 * k
        ncx, ncy = w - 8 - nr, 8 + nr
        c.create_oval(ncx - nr, ncy - nr, ncx + nr, ncy + nr, fill=p["overlay_bg"], outline=p["overlay_border"])
        c.create_line(ncx, ncy + nr * 0.65, ncx, ncy - nr * 0.65, arrow="last", width=2, fill=p["overlay_fg"],
                      arrowshape=(7 * k, 8 * k, 3 * k))
        self._label(ncx, ncy + nr + 4, "N", anchor="n", font=F.small_bold, pad=1)
        below_north = ncy + nr + line + 12

        nice = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000]
        target = (w * 0.18) * mpp
        seg_m = max([v for v in nice if v <= target] or [nice[0]])
        px = seg_m / mpp
        sb_text = ("%g km" % (seg_m / 1000)) if seg_m >= 1000 else "%d m" % seg_m
        sb_h = line + 10
        sy = h - 6 - sb_h / 2
        self._box(6, h - 6 - sb_h, 22 + px + F.small.measure(sb_text) + 8, h - 6)
        c.create_line(12, sy, 12 + px, sy, width=4, fill=p["overlay_fg"])
        c.create_line(12, sy - 5, 12, sy + 5, width=2, fill=p["overlay_fg"])
        c.create_line(12 + px, sy - 5, 12 + px, sy + 5, width=2, fill=p["overlay_fg"])
        c.create_text(18 + px, sy, anchor="w", font=F.small, text=sb_text, fill=p["overlay_fg"])

        if provider:
            attr = basemap.PROVIDERS[provider]["attribution"]
            if self.tiles.errors and not self.tiles.successes and not self._tile_images:
                attr = "Base map unavailable (offline?) · " + attr
            self._label(w - 4, h - 4, attr, anchor="se", font=F.small, pad=2)

        try:
            wind = self.read_wind()
        except ValueError:
            wind = None
        if wind and not wind.is_calm():
            s, d = wind.at(self._typical_alt())
            text = "Wind %.0f %s from %03d°" % (s / self._speed_factor(), self.speed_unit.get(), round(d) % 360)
            L = 22 * k
            bw = max(F.small_bold.measure(text) + 16, 2 * L + 30)
            bx1, by0 = w - 8, below_north
            bx0, by1 = bx1 - bw, by0 + 2 * L + 16 + F.line(F.small_bold)
            self._box(bx0, by0, bx1, by1)
            ax, ay = (bx0 + bx1) / 2, by0 + 8 + L
            a = math.radians(d + 180)
            c.create_line(ax - L * math.sin(a), ay + L * math.cos(a), ax + L * math.sin(a), ay - L * math.cos(a),
                          arrow="last", width=5 * k, fill=COLOR_NEUTRAL, arrowshape=(13 * k, 16 * k, 7 * k))
            c.create_text(ax, by1 - 5, text=text, anchor="s", font=F.small_bold, fill=p["overlay_fg"])

    # ------------------------------------------------------------------ export / help
    def export_csv(self):
        if not self.windy:
            messagebox.showinfo(APP_NAME, "Load a flight plan and calculate first.")
            return
        path = filedialog.asksaveasfilename(title="Export results", defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        sf, af = self._speed_factor(), self._alt_factor()
        su, au = self.speed_unit.get(), self.alt_unit.get()
        with open(path, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["Segment", "Type", "Distance (m)", "Alt start (%s)" % au, "Alt end (%s)" % au,
                         "Course (deg)", "Heading (deg)", "Wind speed (%s)" % su, "Wind from (deg)",
                         "Airspeed (%s)" % su, "GS calm (%s)" % su, "GS wind (%s)" % su,
                         "Time calm (s)", "Time wind (s)", "Note"])
            for a, b in zip(self.calm.segments, self.windy.segments):
                wr.writerow([b.label, b.kind, round(b.distance_m, 1), round(b.alt_start / af, 1),
                             round(b.alt_end / af, 1),
                             "" if b.course_deg is None else round(b.course_deg),
                             "" if b.heading_deg is None else round(b.heading_deg),
                             round(b.wind_speed / sf, 1), round(b.wind_from),
                             "" if b.airspeed is None else round(b.airspeed / sf, 1),
                             "" if a.groundspeed is None else round(a.groundspeed / sf, 1),
                             "" if b.groundspeed is None else round(b.groundspeed / sf, 1),
                             round(a.time_s, 1), round(b.time_s, 1) if math.isfinite(b.time_s) else "inf", b.note])
            wr.writerow([])
            wr.writerow(["TOTAL", "", round(self.calm.total_distance_m, 1), "", "", "", "", "", "", "", "", "",
                         round(self.calm.total_time_s, 1), round(self.windy.total_time_s, 1)])
        self.status.set("Exported results to %s" % path)

    def show_help(self):
        messagebox.showinfo("How the estimate works", (
            "Each leg is flown at the cruise airspeed. The wind triangle is solved for the leg's course to get "
            "ground speed (the aircraft crabs into crosswinds). If MIN ground speed is set, airspeed is raised "
            "toward MAX airspeed in strong headwinds, like ArduPlane.\n\n"
            "Climbs and descents use the climb / descent rates; a leg takes whichever is longer, the horizontal "
            "or the vertical part.\n\n"
            "Loiters integrate ground speed around the circle. DO_JUMP loops, DO_CHANGE_SPEED, VTOL take-off, "
            "transitions and VTOL landing are simulated.\n\n"
            "With layered winds, each leg uses the wind interpolated at its mid-height above launch.\n\n"
            "Wind sources: Open-Meteo forecast (worldwide, winds at 10-180 m and pressure levels) and NOAA "
            "METAR surface observations. Real-world time also depends on turns, gusts and the autopilot, so keep "
            "a healthy reserve."))

    def show_about(self):
        messagebox.showinfo("About", "%s %s\n\nFlight time estimator for fixed-wing and VTOL missions.\n"
                                     "Weather data: Open-Meteo.com (CC BY 4.0) and NOAA aviationweather.gov."
                            % (APP_NAME, __version__))

    # ------------------------------------------------------------------ settings
    def _load_settings(self):
        try:
            with open(settings_path(), encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            return
        try:
            # Settings from version 1 defaulted to knots; start those users on metric units.
            current = s.get("settings_version", 1) >= SETTINGS_VERSION
            if current:
                self.speed_unit.set(s.get("speed_unit", "km/h"))
                self.alt_unit.set(s.get("alt_unit", "m"))
                self._prev_units = (self.speed_unit.get(), self.alt_unit.get())
            if "aircraft" in s:
                self.preset.set(s["aircraft"].get("name", self.preset.get()))
                self._apply_profile(AircraftProfile.from_dict(s["aircraft"]))
            if current:
                self.wind_speed.set(s.get("wind_speed", self.wind_speed.get()))
                self.kml_alt.set(s.get("kml_alt", self.kml_alt.get()))
            self.wind_dir.set(s.get("wind_dir", self.wind_dir.get()))
            if s.get("appearance") in theme.APPEARANCES:
                self.appearance.set(s["appearance"])
            if s.get("map_provider") in list(basemap.PROVIDERS) + [basemap.NO_MAP]:
                self.map_provider.set(s["map_provider"])
            self.kml_takeoff.set(s.get("kml_takeoff", True))
            self.kml_rtl.set(s.get("kml_rtl", True))
            self.kml_first_is_launch.set(s.get("kml_first_is_launch", True))
            geom = s.get("geometry")
            if geom:
                self.root.geometry(geom)
        except Exception:
            pass

    def _save_settings(self):
        s = {"settings_version": SETTINGS_VERSION, "map_provider": self.map_provider.get(),
             "appearance": self.appearance.get(),
             "speed_unit": self.speed_unit.get(), "alt_unit": self.alt_unit.get(),
             "wind_speed": self.wind_speed.get(), "wind_dir": self.wind_dir.get(),
             "kml_alt": self.kml_alt.get(), "kml_takeoff": self.kml_takeoff.get(), "kml_rtl": self.kml_rtl.get(),
             "kml_first_is_launch": self.kml_first_is_launch.get(), "geometry": self.root.geometry()}
        try:
            s["aircraft"] = json.loads(self.read_profile().to_json())
        except ValueError:
            pass
        try:
            os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except OSError:
            pass

    def _on_close(self):
        self._save_settings()
        self.root.destroy()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    root.geometry("1280x820")
    initial = next((a for a in argv if os.path.isfile(a)), None)
    App(root, initial)
    root.mainloop()


if __name__ == "__main__":
    main()
