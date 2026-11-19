"""Wind model: layered wind profile and wind-triangle maths.

Conventions: speeds in m/s, wind direction is the direction the wind blows
FROM (meteorological convention), degrees true. Altitudes in metres above
ground level (AGL).
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class WindLayer:
    alt_agl_m: float
    speed_ms: float
    direction_from_deg: float


@dataclass
class WindProfile:
    layers: List[WindLayer] = field(default_factory=list)
    source: str = "Manual"

    @classmethod
    def calm(cls):
        return cls([WindLayer(0.0, 0.0, 0.0)], source="Zero wind")

    @classmethod
    def uniform(cls, speed_ms, direction_from_deg, source="Manual"):
        return cls([WindLayer(0.0, speed_ms, direction_from_deg)], source=source)

    def is_calm(self):
        return all(layer.speed_ms <= 0 for layer in self.layers)

    def rotated(self, offset_deg):
        """Copy of the profile with every direction rotated (for best/worst-case sweeps)."""
        return WindProfile(
            [WindLayer(l.alt_agl_m, l.speed_ms, (l.direction_from_deg + offset_deg) % 360) for l in self.layers],
            source=self.source,
        )

    def at(self, alt_agl_m) -> Tuple[float, float]:
        """(speed m/s, direction-from deg) at an altitude, interpolating wind vectors linearly."""
        if not self.layers:
            return 0.0, 0.0
        layers = sorted(self.layers, key=lambda l: l.alt_agl_m)
        if alt_agl_m <= layers[0].alt_agl_m:
            return layers[0].speed_ms, layers[0].direction_from_deg
        if alt_agl_m >= layers[-1].alt_agl_m:
            return layers[-1].speed_ms, layers[-1].direction_from_deg
        for lo, hi in zip(layers, layers[1:]):
            if lo.alt_agl_m <= alt_agl_m <= hi.alt_agl_m:
                span = hi.alt_agl_m - lo.alt_agl_m
                f = 0.0 if span <= 0 else (alt_agl_m - lo.alt_agl_m) / span
                u1, v1 = _to_uv(lo.speed_ms, lo.direction_from_deg)
                u2, v2 = _to_uv(hi.speed_ms, hi.direction_from_deg)
                return _from_uv(u1 + f * (u2 - u1), v1 + f * (v2 - v1))
        return layers[-1].speed_ms, layers[-1].direction_from_deg


def _to_uv(speed, from_deg):
    """Vector the air moves toward: (east, north) components."""
    to = math.radians(from_deg + 180.0)
    return speed * math.sin(to), speed * math.cos(to)


def _from_uv(u, v):
    speed = math.hypot(u, v)
    if speed < 1e-9:
        return 0.0, 0.0
    to = math.degrees(math.atan2(u, v))
    return speed, (to + 180.0) % 360.0


def wind_components(course_deg, wind_speed, wind_from_deg):
    """(along-track tailwind component, cross-track component) for a course."""
    angle = math.radians((wind_from_deg + 180.0) - course_deg)
    return wind_speed * math.cos(angle), wind_speed * math.sin(angle)


def ground_speed(airspeed, course_deg, wind_speed, wind_from_deg) -> Optional[float]:
    """Ground speed along a desired course, or None if the track cannot be held."""
    if wind_speed <= 0:
        return airspeed if airspeed > 0 else None
    along, cross = wind_components(course_deg, wind_speed, wind_from_deg)
    if abs(cross) >= airspeed:
        return None
    gs = math.sqrt(airspeed ** 2 - cross ** 2) + along
    return gs if gs > 0.05 else None


def heading_for_course(airspeed, course_deg, wind_speed, wind_from_deg) -> Optional[float]:
    """Heading to fly (degrees true) to make good the course."""
    if wind_speed <= 0:
        return course_deg % 360
    _, cross = wind_components(course_deg, wind_speed, wind_from_deg)
    if abs(cross) >= airspeed:
        return None
    return (course_deg - math.degrees(math.asin(cross / airspeed))) % 360


def airspeed_for_groundspeed(groundspeed, course_deg, wind_speed, wind_from_deg):
    """Airspeed needed to achieve a ground speed along a course."""
    wu, wv = _to_uv(wind_speed, wind_from_deg)
    c = math.radians(course_deg)
    gu, gv = groundspeed * math.sin(c), groundspeed * math.cos(c)
    return math.hypot(gu - wu, gv - wv)
