"""Aircraft performance profiles (all values SI: m, m/s, s)."""

import json
from dataclasses import asdict, dataclass, fields

FIXED_WING = "fixed_wing"
VTOL = "vtol"


@dataclass
class AircraftProfile:
    name: str = "Generic fixed wing"
    kind: str = FIXED_WING

    # Forward flight (ArduPlane: AIRSPEED_CRUISE / TRIM_ARSPD_CM, AIRSPEED_MAX, MIN_GROUNDSPD)
    cruise_airspeed: float = 18.0
    max_airspeed: float = 25.0
    min_groundspeed: float = 0.0
    climb_rate: float = 3.0           # TECS_CLMB_MAX
    sink_rate: float = 2.5            # TECS_SINK_MAX
    loiter_radius: float = 60.0       # WP_LOITER_RAD
    rtl_altitude: float = 100.0       # RTL_ALTITUDE; <0 keeps current altitude
    include_fw_landing: bool = True   # add descent time after fixed-wing RTL

    # VTOL / QuadPlane (Q_WP_SPEED, Q_WP_SPEED_UP, Q_WP_SPEED_DN, Q_LAND_FINAL_ALT, Q_LAND_FINAL_SPD)
    vtol_horizontal_speed: float = 5.0
    vtol_climb_rate: float = 2.5
    vtol_descent_rate: float = 1.5
    vtol_land_final_alt: float = 6.0
    vtol_land_final_speed: float = 0.5
    forward_transition_time: float = 10.0
    back_transition_time: float = 8.0
    takeoff_land_as_vtol: bool = False   # treat NAV_TAKEOFF / NAV_LAND as VTOL (Q_OPTIONS)
    rtl_vtol_land: bool = True           # Q_RTL_MODE: VTOL landing at end of RTL

    # Endurance / reserve check (minutes, 0 disables)
    endurance_min: float = 0.0

    @property
    def is_vtol(self):
        return self.kind == VTOL

    def to_json(self):
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_dict(cls, data):
        known = {f.name: f for f in fields(cls)}
        kwargs = {}
        for k, v in data.items():
            if k in known:
                t = known[k].type
                if t in (bool, "bool"):
                    kwargs[k] = bool(v)
                elif t in (float, "float"):
                    kwargs[k] = float(v)
                else:
                    kwargs[k] = v
        return cls(**kwargs)

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(json.loads(text))


PRESETS = {
    "Generic fixed wing": AircraftProfile(),
    "Generic VTOL (QuadPlane)": AircraftProfile(
        name="Generic VTOL (QuadPlane)", kind=VTOL, cruise_airspeed=20.0, max_airspeed=26.0,
        climb_rate=3.0, sink_rate=2.5, loiter_radius=80.0, rtl_altitude=100.0),
    "Small trainer / foam wing": AircraftProfile(
        name="Small trainer / foam wing", cruise_airspeed=13.0, max_airspeed=18.0,
        climb_rate=2.5, sink_rate=2.0, loiter_radius=40.0, rtl_altitude=80.0),
    "Long-range fixed wing": AircraftProfile(
        name="Long-range fixed wing", cruise_airspeed=24.0, max_airspeed=32.0,
        climb_rate=4.0, sink_rate=3.0, loiter_radius=120.0, rtl_altitude=150.0),
}
