"""Open-source base map tiles (OpenStreetMap / OpenTopoMap) for the route view.

Tiles are standard 256 px Web-Mercator ("slippy map") PNGs. They are downloaded in
background threads (at most two connections, per the OpenStreetMap tile usage
policy) and cached on disk so repeated views work offline and don't reload the servers.
"""

import math
import os
import queue
import ssl
import sys
import threading
import time
import urllib.request

TILE_SIZE = 256
USER_AGENT = "FlightTimeCalc/1.1 (desktop flight-time estimator; Python urllib)"
CACHE_MAX_AGE_S = 30 * 24 * 3600

PROVIDERS = {
    "OpenStreetMap": {
        "urls": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        "max_zoom": 19,
        "attribution": "© OpenStreetMap contributors",
    },
    "OpenTopoMap (terrain)": {
        "urls": ["https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
                 "https://b.tile.opentopomap.org/{z}/{x}/{y}.png",
                 "https://c.tile.opentopomap.org/{z}/{x}/{y}.png"],
        "max_zoom": 17,
        "attribution": "© OpenStreetMap contributors, SRTM | style © OpenTopoMap (CC-BY-SA)",
    },
}
NO_MAP = "None"


# ---------------------------------------------------------------------------
# Web-Mercator maths (world pixel coordinates at zoom z)
# ---------------------------------------------------------------------------

def world_px(lat, lon, z):
    n = TILE_SIZE * (2 ** z)
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def latlon_from_px(x, y, z):
    n = TILE_SIZE * (2 ** z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def metres_per_px(lat, z):
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)


def fit_zoom(bounds, width, height, margin=50, max_zoom=19):
    """Largest integer zoom at which (min_lat, min_lon, max_lat, max_lon) fits the view."""
    min_lat, min_lon, max_lat, max_lon = bounds
    for z in range(max_zoom, 0, -1):
        x0, y0 = world_px(max_lat, min_lon, z)
        x1, y1 = world_px(min_lat, max_lon, z)
        if (x1 - x0) <= width - 2 * margin and (y1 - y0) <= height - 2 * margin:
            return z
    return 1


# ---------------------------------------------------------------------------
# Tile cache / downloader
# ---------------------------------------------------------------------------

def cache_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "FlightTimeCalc", "tiles")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Caches", "FlightTimeCalc", "tiles")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "FlightTimeCalc", "tiles")


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class TileLoader:
    """Fetches tiles in the background. ``ready`` receives keys of tiles now on disk."""

    def __init__(self, workers=2, root=None):
        self.root = root or cache_dir()
        self.ready = queue.Queue()
        self._jobs = []                       # LIFO: newest view first
        self._pending = set()
        self._failed = {}                     # key -> time of failure
        self._lock = threading.Condition()
        self._ctx = None
        self.errors = 0
        self.successes = 0
        for _ in range(workers):
            threading.Thread(target=self._worker, daemon=True).start()

    def path(self, provider, z, x, y):
        safe = "".join(c if c.isalnum() else "_" for c in provider)
        return os.path.join(self.root, safe, str(z), str(x), "%d.png" % y)

    def cached(self, provider, z, x, y):
        p = self.path(provider, z, x, y)
        try:
            return p if time.time() - os.path.getmtime(p) < CACHE_MAX_AGE_S else None
        except OSError:
            return None

    def stale(self, provider, z, x, y):
        p = self.path(provider, z, x, y)
        return p if os.path.exists(p) else None

    def request(self, provider, z, x, y):
        key = (provider, z, x, y)
        with self._lock:
            if key in self._pending:
                return
            if time.time() - self._failed.get(key, 0) < 60:
                return
            self._pending.add(key)
            self._jobs.append(key)
            self._lock.notify()

    def cancel_all(self):
        with self._lock:
            for k in self._jobs:
                self._pending.discard(k)
            self._jobs.clear()

    def _worker(self):
        while True:
            with self._lock:
                while not self._jobs:
                    self._lock.wait()
                key = self._jobs.pop()
            ok = self._download(key)
            with self._lock:
                self._pending.discard(key)
                if not ok:
                    self._failed[key] = time.time()
            if ok:
                self.ready.put(key)

    def _download(self, key):
        provider, z, x, y = key
        spec = PROVIDERS.get(provider)
        if not spec:
            return False
        urls = spec["urls"]
        url = urls[(x + y) % len(urls)].format(z=z, x=x, y=y)
        if self._ctx is None:
            self._ctx = _ssl_context()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15, context=self._ctx) as resp:
                data = resp.read()
            if not data.startswith(b"\x89PNG"):
                raise ValueError("not a PNG tile")
            p = self.path(*key)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            self.successes += 1
            return True
        except Exception:
            self.errors += 1
            return False
