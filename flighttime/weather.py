"""Fetch current / forecast winds from free public sources (no API key needed).

* Open-Meteo (https://open-meteo.com) - worldwide forecast, winds at 10/80/120/180 m
  above ground plus pressure levels up to ~3 km.
* NOAA Aviation Weather Center METAR (https://aviationweather.gov) - observed surface
  wind at the nearest reporting airport.
"""

import json
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List

from .geo import distance_m
from .wind import WindLayer, WindProfile

USER_AGENT = "FlightTimeCalc/1.0 (desktop flight-time estimator)"
KNOT = 0.514444

SURFACE_LEVELS_M = (10, 80, 120, 180)
PRESSURE_LEVELS_HPA = (1000, 975, 950, 925, 900, 850, 800, 700)


class WeatherError(Exception):
    pass


@dataclass
class WindForecast:
    source: str
    lat: float
    lon: float
    elevation_m: float
    times: List[datetime] = field(default_factory=list)       # UTC
    profiles: List[WindProfile] = field(default_factory=list)

    def nearest_index(self, when=None):
        when = when or datetime.now(timezone.utc)
        return min(range(len(self.times)), key=lambda i: abs((self.times[i] - when).total_seconds()))


def _ssl_context():
    try:
        import certifi  # bundled in the standalone builds
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            body = resp.read().decode("utf-8")
    except Exception as e:  # network errors, HTTP errors, timeouts
        raise WeatherError("Could not reach %s: %s" % (urllib.parse.urlparse(url).netloc, e))
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except ValueError:
        raise WeatherError("Unexpected response from %s" % urllib.parse.urlparse(url).netloc)


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------

def open_meteo_url(lat, lon, forecast_days=3):
    hourly = []
    for h in SURFACE_LEVELS_M:
        hourly += ["wind_speed_%dm" % h, "wind_direction_%dm" % h]
    for p in PRESSURE_LEVELS_HPA:
        hourly += ["wind_speed_%dhPa" % p, "wind_direction_%dhPa" % p, "geopotential_height_%dhPa" % p]
    q = {
        "latitude": "%.4f" % lat,
        "longitude": "%.4f" % lon,
        "hourly": ",".join(hourly),
        "wind_speed_unit": "ms",
        "timezone": "GMT",
        "forecast_days": str(forecast_days),
        "past_days": "1",
    }
    return "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q)


def parse_open_meteo(data, lat, lon):
    if not data or "hourly" not in data:
        reason = (data or {}).get("reason", "no data") if isinstance(data, dict) else "no data"
        raise WeatherError("Open-Meteo returned no forecast (%s)." % reason)
    hourly = data["hourly"]
    elevation = float(data.get("elevation") or 0.0)
    times = [datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) for t in hourly.get("time", [])]

    def val(key, i):
        arr = hourly.get(key)
        if not arr or i >= len(arr):
            return None
        return arr[i]

    profiles = []
    for i in range(len(times)):
        layers = []
        for h in SURFACE_LEVELS_M:
            s, d = val("wind_speed_%dm" % h, i), val("wind_direction_%dm" % h, i)
            if s is not None and d is not None:
                layers.append(WindLayer(float(h), float(s), float(d)))
        top = max([l.alt_agl_m for l in layers] or [0.0])
        for p in PRESSURE_LEVELS_HPA:
            s, d, z = (val("wind_speed_%dhPa" % p, i), val("wind_direction_%dhPa" % p, i),
                       val("geopotential_height_%dhPa" % p, i))
            if s is None or d is None or z is None:
                continue
            agl = float(z) - elevation
            if agl > top + 20:   # only levels above the surface-based ones
                layers.append(WindLayer(round(agl), float(s), float(d)))
                top = agl
        if layers:
            profiles.append(WindProfile(layers, source="Open-Meteo forecast"))
        else:
            profiles.append(None)
    keep = [(t, p) for t, p in zip(times, profiles) if p is not None]
    if not keep:
        raise WeatherError("Open-Meteo returned no wind values for this location.")
    return WindForecast("Open-Meteo forecast", lat, lon, elevation, [k[0] for k in keep], [k[1] for k in keep])


def fetch_open_meteo(lat, lon, timeout=20):
    return parse_open_meteo(_get_json(open_meteo_url(lat, lon), timeout), lat, lon)


# ---------------------------------------------------------------------------
# NOAA METAR
# ---------------------------------------------------------------------------

@dataclass
class MetarWind:
    station: str
    name: str
    distance_km: float
    speed_ms: float
    direction_from_deg: float
    gust_ms: float
    variable: bool
    observed: str
    raw: str

    def profile(self):
        return WindProfile([WindLayer(10.0, self.speed_ms, self.direction_from_deg)],
                           source="METAR %s (%.0f km away)" % (self.station, self.distance_km))


def metar_url(lat, lon, radius_deg):
    bbox = "%.3f,%.3f,%.3f,%.3f" % (lat - radius_deg, lon - radius_deg, lat + radius_deg, lon + radius_deg)
    return "https://aviationweather.gov/api/data/metar?" + urllib.parse.urlencode(
        {"bbox": bbox, "format": "json", "hours": "2"})


def parse_metars(data, lat, lon):
    best = None
    for rec in data or []:
        try:
            slat, slon = float(rec["lat"]), float(rec["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        wspd = rec.get("wspd")
        if wspd is None:
            continue
        wdir = rec.get("wdir")
        variable = not isinstance(wdir, (int, float))
        d = distance_m(lat, lon, slat, slon) / 1000.0
        cand = MetarWind(
            station=str(rec.get("icaoId", "?")),
            name=str(rec.get("name", "") or ""),
            distance_km=d,
            speed_ms=float(wspd) * KNOT,
            direction_from_deg=0.0 if variable else float(wdir),
            gust_ms=float(rec.get("wgst") or 0) * KNOT,
            variable=variable,
            observed=str(rec.get("reportTime") or rec.get("obsTime") or ""),
            raw=str(rec.get("rawOb", "")),
        )
        if best is None or cand.distance_km < best.distance_km - 0.01 or (
                abs(cand.distance_km - best.distance_km) <= 0.01 and cand.observed > best.observed):
            best = cand
    return best


def fetch_nearest_metar(lat, lon, timeout=20):
    for radius in (0.75, 2.0, 4.0):
        best = parse_metars(_get_json(metar_url(lat, lon, radius), timeout), lat, lon)
        if best:
            return best
    raise WeatherError("No METAR stations found within ~400 km. (METAR coverage is best in North America.)")
