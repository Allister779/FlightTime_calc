"""Mission flight-time estimator.

Walks the mission the way ArduPlane / QuadPlane would execute it (including
DO_JUMP loops, speed changes and VTOL transitions) and adds up the time of
every segment, solving the wind triangle on every leg.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional

from . import mission as M
from .aircraft import AircraftProfile
from .geo import bearing_deg, distance_m
from .wind import WindProfile, airspeed_for_groundspeed, ground_speed, heading_for_course

INF = float("inf")


@dataclass
class Segment:
    label: str
    kind: str                      # leg, climb, descent, loiter, transition, hold
    seq: Optional[int] = None
    start: Optional[tuple] = None  # (lat, lon)
    end: Optional[tuple] = None
    distance_m: float = 0.0
    course_deg: Optional[float] = None
    heading_deg: Optional[float] = None
    alt_start: float = 0.0
    alt_end: float = 0.0
    airspeed: Optional[float] = None
    groundspeed: Optional[float] = None
    wind_speed: float = 0.0
    wind_from: float = 0.0
    time_s: float = 0.0
    loiter_radius: Optional[float] = None
    note: str = ""


@dataclass
class Estimate:
    segments: List[Segment] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def total_time_s(self):
        return sum(s.time_s for s in self.segments)

    @property
    def total_distance_m(self):
        return sum(s.distance_m for s in self.segments)

    @property
    def feasible(self):
        return math.isfinite(self.total_time_s)


class _Sim:
    def __init__(self, mission, aircraft: AircraftProfile, wind: WindProfile):
        self.m = mission
        self.ac = aircraft
        self.wind = wind
        self.est = Estimate()
        self.lat, self.lon = mission.home_lat, mission.home_lon
        self.alt = 0.0
        self.airborne = False
        self.mc_mode = False           # QuadPlane flying in multicopter (VTOL) mode
        self.mc_forced = False         # DO_VTOL_TRANSITION to MC requested
        self.airspeed = aircraft.cruise_airspeed
        self.vtol_speed = aircraft.vtol_horizontal_speed
        self.min_gs = aircraft.min_groundspeed
        self.finished = False
        self._warned = set()

    # -- helpers ---------------------------------------------------------------
    def warn(self, text):
        if text not in self._warned:
            self._warned.add(text)
            self.est.warnings.append(text)

    def add(self, seg):
        self.est.segments.append(seg)

    def wind_at(self, alt):
        return self.wind.at(max(alt, 0.0))

    def _speed_on_course(self, course, alt):
        """(airspeed, groundspeed, heading) flying ``course`` at ``alt``."""
        ws, wd = self.wind_at(alt)
        if self.mc_mode:
            # Multicopter mode holds a ground-speed target (Q_WP_SPEED) if it can.
            tas = self.vtol_speed
            gs_target = self.vtol_speed
            need = airspeed_for_groundspeed(gs_target, course, ws, wd)
            # Copter-style controllers can tilt into the wind up to ~2x the waypoint speed.
            if need <= max(2.0 * tas, tas + 8.0):
                return need, gs_target, heading_for_course(need, course, ws, wd) if need > 0 else course
            return need, None, None
        tas = self.airspeed
        gs = ground_speed(tas, course, ws, wd)
        if self.min_gs > 0 and (gs is None or gs < self.min_gs):
            # ArduPlane raises airspeed (up to AIRSPEED_MAX) to keep MIN_GROUNDSPD.
            need = airspeed_for_groundspeed(self.min_gs, course, ws, wd)
            tas = min(max(need, tas), max(self.ac.max_airspeed, tas))
            gs = ground_speed(tas, course, ws, wd)
        return tas, gs, heading_for_course(tas, course, ws, wd)

    def ensure_airborne(self, seq):
        if not self.airborne:
            self.airborne = True
            self.warn("No take-off command before the first waypoint; assumed a hand/catapult launch.")

    def ensure_mode_for_leg(self):
        """QuadPlanes transition to forward flight before a normal leg unless forced to stay in VTOL mode."""
        if self.ac.is_vtol and self.mc_mode and not self.mc_forced:
            self.add(Segment("Forward transition", "transition", time_s=self.ac.forward_transition_time,
                             alt_start=self.alt, alt_end=self.alt, start=(self.lat, self.lon),
                             end=(self.lat, self.lon)))
            self.mc_mode = False

    def back_transition(self):
        if self.ac.is_vtol and not self.mc_mode:
            self.add(Segment("Back transition", "transition", time_s=self.ac.back_transition_time,
                             alt_start=self.alt, alt_end=self.alt, start=(self.lat, self.lon),
                             end=(self.lat, self.lon)))
            self.mc_mode = True

    # -- primitives ------------------------------------------------------------
    def fly_to(self, lat, lon, alt, label, seq=None):
        self.ensure_airborne(seq)
        self.ensure_mode_for_leg()
        dist = distance_m(self.lat, self.lon, lat, lon)
        dz = alt - self.alt
        if self.mc_mode:
            v_rate = self.ac.vtol_climb_rate if dz > 0 else self.ac.vtol_descent_rate
        else:
            v_rate = self.ac.climb_rate if dz > 0 else self.ac.sink_rate
        t_vert = abs(dz) / v_rate if v_rate > 0 else 0.0
        seg = Segment(label, "leg", seq, (self.lat, self.lon), (lat, lon), dist,
                      alt_start=self.alt, alt_end=alt)
        if dist < 0.5:
            seg.kind = "climb" if dz > 0 else ("descent" if dz < 0 else "leg")
            seg.time_s = t_vert
        else:
            course = bearing_deg(self.lat, self.lon, lat, lon)
            mid_alt = (self.alt + alt) / 2.0
            ws, wd = self.wind_at(mid_alt)
            tas, gs, hdg = self._speed_on_course(course, mid_alt)
            seg.course_deg, seg.heading_deg, seg.airspeed, seg.groundspeed = course, hdg, tas, gs
            seg.wind_speed, seg.wind_from = ws, wd
            if gs is None:
                seg.time_s = INF
                seg.note = "Wind too strong to hold this track"
                self.warn("Leg '%s' cannot be flown: the wind exceeds what the aircraft can hold." % label)
            else:
                t_h = dist / gs
                seg.time_s = max(t_h, t_vert)
                if t_vert > t_h * 1.01:
                    seg.note = "Climb/descent limited"
        self.add(seg)
        self.lat, self.lon, self.alt = lat, lon, alt

    def vertical(self, target_alt, label, seq=None, vtol=False, kind=None):
        dz = target_alt - self.alt
        if vtol:
            if dz >= 0:
                t = dz / self.ac.vtol_climb_rate
            else:
                final = max(self.ac.vtol_land_final_alt, 0.0) if target_alt <= 0.01 else 0.0
                upper = max(self.alt - max(final, target_alt), 0.0)
                lower = max(min(self.alt, final) - target_alt, 0.0)
                t = upper / self.ac.vtol_descent_rate
                t += lower / self.ac.vtol_land_final_speed if self.ac.vtol_land_final_speed > 0 else 0.0
        else:
            rate = self.ac.climb_rate if dz >= 0 else self.ac.sink_rate
            t = abs(dz) / rate
        ws, wd = self.wind_at(max(self.alt, target_alt))
        self.add(Segment(label, kind or ("climb" if dz >= 0 else "descent"), seq, (self.lat, self.lon),
                         (self.lat, self.lon), 0.0, alt_start=self.alt, alt_end=target_alt,
                         wind_speed=ws, wind_from=wd, time_s=t))
        self.alt = target_alt

    def loiter_circle_time(self, radius, alt):
        """Time for one full orbit of ``radius`` at ``alt``, integrating ground speed round the circle."""
        radius = abs(radius) if radius else self.ac.loiter_radius
        n = 72
        ds = 2 * math.pi * radius / n
        t = 0.0
        for k in range(n):
            course = (k + 0.5) * 360.0 / n
            _, gs, _ = self._speed_on_course(course, alt)
            if gs is None:
                return INF
            t += ds / gs
        return t

    def loiter(self, label, seq, turns=None, seconds=None, radius=None):
        radius = abs(radius) if radius else self.ac.loiter_radius
        ws, wd = self.wind_at(self.alt)
        seg = Segment(label, "loiter", seq, (self.lat, self.lon), (self.lat, self.lon), 0.0,
                      alt_start=self.alt, alt_end=self.alt, wind_speed=ws, wind_from=wd,
                      airspeed=self.vtol_speed if self.mc_mode else self.airspeed, loiter_radius=radius)
        if seconds is not None:
            seg.time_s = max(seconds, 0.0)
            seg.note = "%.0f s loiter" % seconds
        else:
            t1 = self.loiter_circle_time(radius, self.alt)
            seg.time_s = turns * t1
            seg.distance_m = turns * 2 * math.pi * radius
            seg.note = "%g turn(s), r=%.0f m" % (turns, radius)
            if not math.isfinite(t1):
                self.warn("Loiter at item %s cannot be held in this wind." % seq)
        self.add(seg)

    # -- landing ---------------------------------------------------------------
    def vtol_land(self, lat, lon, label, seq):
        if lat is not None:
            self.fly_to(lat, lon, self.alt, label + " (approach)", seq)
        self.back_transition()
        self.vertical(0.0, label + " (vertical descent)", seq, vtol=True, kind="descent")
        self.airborne = False
        self.finished = True

    def fw_land(self, lat, lon, label, seq):
        if lat is not None and distance_m(self.lat, self.lon, lat, lon) > 0.5:
            self.fly_to(lat, lon, 0.0, label + " (approach)", seq)
        else:
            self.vertical(0.0, label + " (descent)", seq)
        self.airborne = False
        self.finished = True

    # -- main loop -------------------------------------------------------------
    def run(self):
        items = self.m.items
        index = {it.seq: i for i, it in enumerate(items)}
        jump_counts = {}
        i = 0
        steps = 0
        while i < len(items) and not self.finished:
            steps += 1
            if steps > 20000:
                self.warn("Mission loop limit reached; estimate truncated.")
                break
            it = items[i]
            nxt = i + 1
            cmd = it.command
            label = "#%d %s" % (it.seq, it.name)
            pos = (it.lat, it.lon) if it.has_position else (self.lat, self.lon)
            p1, p2, p3, p4 = it.params

            if cmd in (M.NAV_WAYPOINT, M.NAV_SPLINE_WAYPOINT, M.NAV_PAYLOAD_PLACE):
                self.fly_to(pos[0], pos[1], it.alt, label, it.seq)
                if self.mc_mode and p1 > 0:
                    self.loiter(label + " hold", it.seq, seconds=p1)
            elif cmd == M.NAV_TAKEOFF and not (self.ac.is_vtol and self.ac.takeoff_land_as_vtol):
                self.airborne = True
                self.mc_mode = False
                self.vertical(max(it.alt, self.alt), label, it.seq, kind="climb")
            elif cmd in (M.NAV_VTOL_TAKEOFF, M.NAV_TAKEOFF):
                if not self.ac.is_vtol:
                    self.warn("VTOL_TAKEOFF found but the aircraft is fixed wing; treated as a normal climb-out.")
                    self.airborne = True
                    self.vertical(max(it.alt, self.alt), label, it.seq, kind="climb")
                else:
                    self.airborne = True
                    self.mc_mode = True
                    self.vertical(max(it.alt, self.alt), label, it.seq, vtol=True, kind="climb")
            elif cmd == M.NAV_LOITER_TIME:
                self.fly_to(pos[0], pos[1], it.alt, label + " (to)", it.seq)
                self.loiter(label, it.seq, seconds=p1, radius=p3)
            elif cmd == M.NAV_LOITER_TURNS:
                self.fly_to(pos[0], pos[1], it.alt, label + " (to)", it.seq)
                self.loiter(label, it.seq, turns=max(p1, 0.0), radius=p3)
            elif cmd == M.NAV_LOITER_TO_ALT:
                self.fly_to(pos[0], pos[1], self.alt, label + " (to)", it.seq)
                radius = p2 if p2 else self.ac.loiter_radius
                dz = it.alt - self.alt
                rate = self.ac.climb_rate if dz > 0 else self.ac.sink_rate
                turns_needed = (abs(dz) / rate) / max(self.loiter_circle_time(radius, self.alt), 1e-6)
                self.loiter(label + " (climb/descend)", it.seq, turns=turns_needed, radius=radius)
                self.alt = it.alt
                self.est.segments[-1].alt_end = it.alt
            elif cmd == M.NAV_CONTINUE_AND_CHANGE_ALT:
                self.vertical(it.alt, label, it.seq)
            elif cmd == M.NAV_LOITER_UNLIM:
                self.fly_to(pos[0], pos[1], it.alt, label + " (to)", it.seq)
                self.warn("Mission reaches LOITER_UNLIM at item %d and would loiter indefinitely; "
                          "estimate stops there." % it.seq)
                self.finished = True
            elif cmd == M.NAV_RETURN_TO_LAUNCH:
                rtl_alt = self.ac.rtl_altitude if self.ac.rtl_altitude >= 0 else self.alt
                self.fly_to(self.m.home_lat, self.m.home_lon, rtl_alt, label, it.seq)
                if self.ac.is_vtol and self.ac.rtl_vtol_land:
                    self.vtol_land(None, None, "RTL landing", it.seq)
                elif self.ac.include_fw_landing:
                    self.vertical(0.0, "RTL descent & landing", it.seq)
                    self.finished = True
                else:
                    self.finished = True
            elif cmd == M.NAV_VTOL_LAND or (cmd == M.NAV_LAND and self.ac.is_vtol and self.ac.takeoff_land_as_vtol):
                if not self.ac.is_vtol:
                    self.fw_land(pos[0], pos[1], label, it.seq)
                else:
                    self.vtol_land(pos[0], pos[1], label, it.seq)
            elif cmd == M.NAV_LAND:
                self.fw_land(pos[0], pos[1], label, it.seq)
            elif cmd == M.NAV_DELAY:
                if p1 >= 0:
                    self.loiter(label, it.seq, seconds=p1)
                else:
                    self.warn("DELAY until a time of day (item %d) is not included." % it.seq)
            elif cmd == M.DO_JUMP:
                target = int(p1)
                repeat = int(p2)
                if target in index:
                    done = jump_counts.get(it.seq, 0)
                    if repeat < 0:
                        if done == 0:
                            self.warn("DO_JUMP at item %d repeats forever; the loop is counted once." % it.seq)
                            repeat = 1
                        else:
                            repeat = 0
                    if done < repeat:
                        jump_counts[it.seq] = done + 1
                        nxt = index[target]
                else:
                    self.warn("DO_JUMP at item %d targets missing item %d." % (it.seq, target))
            elif cmd == M.DO_CHANGE_SPEED:
                speed_type, speed = int(p1), p2
                if speed > 0:
                    if speed_type == 1:
                        self.min_gs = speed
                    elif self.mc_mode:
                        self.vtol_speed = speed
                    else:
                        self.airspeed = speed
            elif cmd == M.DO_VTOL_TRANSITION:
                if self.ac.is_vtol:
                    if int(p1) == 3:
                        self.mc_forced = True
                        if self.airborne:
                            self.back_transition()
                    elif int(p1) == 4:
                        self.mc_forced = False
                        if self.airborne:
                            self.ensure_mode_for_leg()
            # Other DO_/CONDITION_ commands do not affect flight time.
            i = nxt
        return self.est


def estimate(mission, aircraft: AircraftProfile, wind: Optional[WindProfile] = None) -> Estimate:
    return _Sim(mission, aircraft, wind or WindProfile.calm()).run()


def direction_sweep(mission, aircraft, wind: WindProfile, step_deg=10):
    """Total time for the same wind blowing from every direction. Returns [(offset_deg, seconds)]."""
    out = []
    for off in range(0, 360, step_deg):
        out.append((off, estimate(mission, aircraft, wind.rotated(off)).total_time_s))
    return out
