"""Flight plan loading.

Supported inputs
  * ArduPilot / Mission Planner waypoint files (``QGC WPL 110`` text: .waypoints, .txt, .mission)
  * QGroundControl ``.plan`` JSON files (ArduPilot missions saved from QGC)
  * Google Earth KML and KMZ (LineString / gx:Track paths, or ordered Point placemarks)

Every format is turned into a :class:`Mission`: an ordered list of MAVLink-style
mission items whose altitudes are metres relative to the home/launch point.
"""

import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# MAVLink command ids used by ArduPlane / QuadPlane missions
NAV_WAYPOINT = 16
NAV_LOITER_UNLIM = 17
NAV_LOITER_TURNS = 18
NAV_LOITER_TIME = 19
NAV_RETURN_TO_LAUNCH = 20
NAV_LAND = 21
NAV_TAKEOFF = 22
NAV_CONTINUE_AND_CHANGE_ALT = 30
NAV_LOITER_TO_ALT = 31
NAV_SPLINE_WAYPOINT = 82
NAV_VTOL_TAKEOFF = 84
NAV_VTOL_LAND = 85
NAV_DELAY = 93
NAV_PAYLOAD_PLACE = 94
DO_JUMP = 177
DO_CHANGE_SPEED = 178
DO_LAND_START = 189
DO_VTOL_TRANSITION = 3000

COMMAND_NAMES = {
    NAV_WAYPOINT: "WAYPOINT",
    NAV_LOITER_UNLIM: "LOITER_UNLIM",
    NAV_LOITER_TURNS: "LOITER_TURNS",
    NAV_LOITER_TIME: "LOITER_TIME",
    NAV_RETURN_TO_LAUNCH: "RETURN_TO_LAUNCH",
    NAV_LAND: "LAND",
    NAV_TAKEOFF: "TAKEOFF",
    NAV_CONTINUE_AND_CHANGE_ALT: "CONTINUE_AND_CHANGE_ALT",
    NAV_LOITER_TO_ALT: "LOITER_TO_ALT",
    NAV_SPLINE_WAYPOINT: "SPLINE_WAYPOINT",
    NAV_VTOL_TAKEOFF: "VTOL_TAKEOFF",
    NAV_VTOL_LAND: "VTOL_LAND",
    NAV_DELAY: "DELAY",
    NAV_PAYLOAD_PLACE: "PAYLOAD_PLACE",
    DO_JUMP: "DO_JUMP",
    DO_CHANGE_SPEED: "DO_CHANGE_SPEED",
    DO_LAND_START: "DO_LAND_START",
    DO_VTOL_TRANSITION: "DO_VTOL_TRANSITION",
}

# MAV_FRAME values
FRAME_GLOBAL = (0, 5)                 # altitude above mean sea level
FRAME_RELATIVE = (3, 6)               # altitude above home
FRAME_TERRAIN = (10, 11)              # altitude above terrain


class MissionParseError(Exception):
    pass


@dataclass
class MissionItem:
    seq: int
    command: int
    lat: float = 0.0
    lon: float = 0.0
    alt: float = 0.0                  # metres relative to home
    params: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    frame: int = 3

    @property
    def name(self):
        return COMMAND_NAMES.get(self.command, "CMD_%d" % self.command)

    @property
    def has_position(self):
        return not (abs(self.lat) < 1e-9 and abs(self.lon) < 1e-9)


@dataclass
class Mission:
    name: str
    source_format: str
    home_lat: float
    home_lon: float
    home_alt_msl: Optional[float]
    items: List[MissionItem] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def positions(self):
        pts = [(self.home_lat, self.home_lon)]
        pts += [(i.lat, i.lon) for i in self.items if i.has_position]
        return pts

    def centroid(self):
        pts = self.positions()
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


@dataclass
class Route:
    """A path found in a KML/KMZ file."""
    name: str
    points: List[Tuple[float, float, Optional[float]]]   # lat, lon, alt (m) or None
    altitude_mode: str = "clampToGround"


# ---------------------------------------------------------------------------
# Generic entry point
# ---------------------------------------------------------------------------

def load_file(path):
    """Load a file. Returns ``("mission", Mission)`` or ``("routes", [Route, ...])``."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        data = f.read()
    name = os.path.splitext(os.path.basename(path))[0]
    if ext == ".kmz" or data[:2] == b"PK":
        return "routes", parse_kmz(data)
    if ext == ".kml":
        return "routes", parse_kml(data)
    text = data.decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return "mission", parse_qgc_plan(text, name)
    if stripped.startswith("<"):
        return "routes", parse_kml(data)
    return "mission", parse_waypoints(text, name)


# ---------------------------------------------------------------------------
# Mission Planner / QGC WPL text format
# ---------------------------------------------------------------------------

def parse_waypoints(text, name="mission"):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        raise MissionParseError("The file is empty.")
    if not lines[0].upper().startswith("QGC WPL"):
        raise MissionParseError("Not a Mission Planner waypoint file (expected a 'QGC WPL 110' header).")
    rows = []
    for n, line in enumerate(lines[1:], start=2):
        parts = re.split(r"\s+", line)
        if len(parts) < 12:
            raise MissionParseError("Line %d has %d columns, expected 12." % (n, len(parts)))
        try:
            rows.append({
                "seq": int(float(parts[0])),
                "frame": int(float(parts[2])),
                "command": int(float(parts[3])),
                "params": tuple(float(p) for p in parts[4:8]),
                "lat": float(parts[8]),
                "lon": float(parts[9]),
                "alt": float(parts[10]),
            })
        except ValueError:
            raise MissionParseError("Line %d contains a non-numeric value." % n)
    if not rows:
        raise MissionParseError("The waypoint file contains no items.")
    return _rows_to_mission(rows, name, "Mission Planner waypoints")


def _rows_to_mission(rows, name, fmt, home=None):
    """Build a Mission from raw rows. ``rows[0]`` is home unless ``home`` is given."""
    warnings = []
    if home is None:
        home_row, rows = rows[0], rows[1:]
        home = (home_row["lat"], home_row["lon"], home_row["alt"])
    home_lat, home_lon, home_alt = home
    if abs(home_lat) < 1e-9 and abs(home_lon) < 1e-9:
        first = next((r for r in rows if abs(r["lat"]) > 1e-9 or abs(r["lon"]) > 1e-9), None)
        if first is None:
            raise MissionParseError("The mission has no positioned waypoints.")
        home_lat, home_lon = first["lat"], first["lon"]
        home_alt = None
        warnings.append("Home position was not set in the file; using the first waypoint as launch point.")
    terrain_warned = False
    items = []
    for r in rows:
        alt = r["alt"]
        if r["frame"] in FRAME_GLOBAL:
            if home_alt is None:
                warnings.append("Item %d uses absolute altitude but home altitude is unknown." % r["seq"])
            else:
                alt = alt - home_alt
        elif r["frame"] in FRAME_TERRAIN and not terrain_warned:
            warnings.append("Terrain-following altitudes are treated as altitude above home.")
            terrain_warned = True
        items.append(MissionItem(r["seq"], r["command"], r["lat"], r["lon"], alt, r["params"], r["frame"]))
    return Mission(name, fmt, home_lat, home_lon, home_alt, items, warnings)


# ---------------------------------------------------------------------------
# QGroundControl .plan JSON
# ---------------------------------------------------------------------------

def parse_qgc_plan(text, name="mission"):
    try:
        doc = json.loads(text)
        mission = doc["mission"]
    except (ValueError, KeyError, TypeError):
        raise MissionParseError("Not a valid QGroundControl .plan file.")
    hp = mission.get("plannedHomePosition") or [0, 0, 0]
    home = (float(hp[0] or 0), float(hp[1] or 0), float(hp[2]) if len(hp) > 2 and hp[2] is not None else None)
    rows = []
    seq = 1
    warnings = []

    def add(cmd, frame, params):
        nonlocal seq
        p = [0.0 if v is None else float(v) for v in (list(params) + [0] * 7)[:7]]
        rows.append({"seq": seq, "frame": frame, "command": cmd, "params": tuple(p[:4]),
                     "lat": p[4], "lon": p[5], "alt": p[6]})
        seq += 1

    for item in mission.get("items", []):
        if item.get("type") == "SimpleItem":
            add(int(item.get("command", 0)), int(item.get("frame", 3)), item.get("params", []))
        elif item.get("type") == "ComplexItem":
            pts = _complex_item_points(item)
            if not pts:
                warnings.append("Skipped a '%s' pattern that has no path." % item.get("complexItemType", "complex"))
                continue
            alt = float(item.get("TransectStyleComplexItem", item).get("CameraCalc", {}).get("DistanceToSurface", 0) or 0)
            if not alt:
                alt = float(item.get("altitude", 0) or 0)
            for lat, lon in pts:
                add(NAV_WAYPOINT, 3, [0, 0, 0, 0, lat, lon, alt])
    m = _rows_to_mission(rows, name, "QGroundControl plan", home=home)
    m.warnings = warnings + m.warnings
    return m


def _complex_item_points(item):
    ts = item.get("TransectStyleComplexItem")
    if ts and ts.get("VisualTransectPoints"):
        return [(float(p[0]), float(p[1])) for p in ts["VisualTransectPoints"]]
    poly = item.get("polygon")
    if poly:
        return [(float(p[0]), float(p[1])) for p in poly]
    return []


# ---------------------------------------------------------------------------
# KML / KMZ
# ---------------------------------------------------------------------------

def parse_kmz(data):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise MissionParseError("The KMZ file is not a valid zip archive.")
    names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
    if not names:
        raise MissionParseError("The KMZ archive does not contain a KML file.")
    names.sort(key=lambda n: (os.path.basename(n).lower() != "doc.kml", n.count("/"), n))
    routes = []
    for n in names:
        try:
            routes += parse_kml(zf.read(n))
        except MissionParseError:
            continue
    if not routes:
        raise MissionParseError("No paths or waypoints were found in the KMZ file.")
    return routes


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text_of(el, name):
    c = _child(el, name)
    return c.text.strip() if c is not None and c.text else ""


def _parse_coords(text):
    pts = []
    for tok in text.split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
            alt = float(parts[2]) if len(parts) > 2 and parts[2] != "" else None
        except ValueError:
            continue
        pts.append((lat, lon, alt))
    return pts


def _altitude_mode(el, default="clampToGround"):
    for c in el.iter():
        if _local(c.tag) == "altitudeMode" and c.text:
            return c.text.strip()
    return default


def parse_kml(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise MissionParseError("Could not read the KML file: %s" % e)
    routes = []
    points = []
    point_mode = None
    for pm in root.iter():
        if _local(pm.tag) != "Placemark":
            continue
        pm_name = _text_of(pm, "name") or "Path %d" % (len(routes) + 1)
        for geom in pm.iter():
            tag = _local(geom.tag)
            if tag in ("LineString", "LinearRing"):
                coords = _child(geom, "coordinates")
                pts = _parse_coords(coords.text if coords is not None and coords.text else "")
                if len(pts) >= 2:
                    routes.append(Route(pm_name, pts, _altitude_mode(geom)))
            elif tag == "Track":   # gx:Track
                pts = []
                for c in geom:
                    if _local(c.tag) == "coord" and c.text:
                        v = c.text.split()
                        if len(v) >= 2:
                            pts.append((float(v[1]), float(v[0]), float(v[2]) if len(v) > 2 else None))
                if len(pts) >= 2:
                    routes.append(Route(pm_name, pts, _altitude_mode(geom)))
            elif tag == "Point":
                coords = _child(geom, "coordinates")
                pts = _parse_coords(coords.text if coords is not None and coords.text else "")
                if pts:
                    points.append(pts[0])
                    point_mode = point_mode or _altitude_mode(geom)
    if len(points) >= 2:
        routes.append(Route("Waypoint placemarks (%d points)" % len(points), points, point_mode or "clampToGround"))
    if not routes:
        raise MissionParseError("No paths (LineString) or waypoint placemarks were found in the KML file.")
    return routes


def route_to_mission(route, default_alt_m=100.0, add_takeoff=True, add_rtl=True, vtol=False,
                     first_point_is_launch=True, name="route"):
    """Convert a KML route into a mission.

    ``first_point_is_launch``: the first coordinate is the take-off spot; otherwise the
    aircraft launches at the first point and flies to it as the first waypoint.
    """
    warnings = []
    pts = list(route.points)
    mode = route.altitude_mode
    has_alt = any(p[2] not in (None, 0.0) for p in pts) and mode != "clampToGround"
    home_lat, home_lon = pts[0][0], pts[0][1]
    home_alt_msl = None
    if mode == "absolute" and has_alt:
        home_alt_msl = pts[0][2] if first_point_is_launch else None
        if home_alt_msl is None:
            home_alt_msl = min(p[2] for p in pts if p[2] is not None)
        warnings.append("KML altitudes are absolute (MSL); launch ground level assumed to be %.0f m MSL "
                        "(the %s)." % (home_alt_msl, "first point" if first_point_is_launch else "lowest point"))

    def rel_alt(p):
        if not has_alt or p[2] is None:
            return default_alt_m
        if home_alt_msl is not None:
            return p[2] - home_alt_msl
        return p[2]

    if not has_alt:
        warnings.append("The path has no altitudes; using the default altitude of %.0f m above launch." % default_alt_m)

    wps = pts[1:] if first_point_is_launch and len(pts) > 1 else pts
    items = []
    seq = 1
    first_alt = max(rel_alt(wps[0]), 10.0) if wps else default_alt_m
    if add_takeoff:
        items.append(MissionItem(seq, NAV_VTOL_TAKEOFF if vtol else NAV_TAKEOFF, 0, 0, first_alt))
        seq += 1
    for p in wps:
        items.append(MissionItem(seq, NAV_WAYPOINT, p[0], p[1], max(rel_alt(p), 0.0)))
        seq += 1
    if add_rtl:
        items.append(MissionItem(seq, NAV_RETURN_TO_LAUNCH))
    return Mission(name, "KML path: %s" % route.name, home_lat, home_lon, home_alt_msl, items, warnings)
