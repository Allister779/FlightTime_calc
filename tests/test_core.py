import math
import os
import unittest

from flighttime import mission as M
from flighttime.aircraft import PRESETS, AircraftProfile, VTOL
from flighttime.estimator import direction_sweep, estimate
from flighttime.weather import parse_metars, parse_open_meteo
from flighttime.wind import WindLayer, WindProfile, ground_speed, heading_for_course

EX = os.path.join(os.path.dirname(__file__), "..", "examples")


def straight_mission(dist_east_m=10000.0):
    lat0, lon0 = 0.0, 0.0
    dlon = dist_east_m / 111195.0
    items = [M.MissionItem(1, M.NAV_WAYPOINT, lat0, lon0 + dlon, 0.0)]
    m = M.Mission("t", "test", lat0, lon0, 0.0, items)
    return m


class WindTriangle(unittest.TestCase):
    def test_head_and_tail(self):
        # Course east (090). Wind FROM east is a headwind.
        self.assertAlmostEqual(ground_speed(20, 90, 5, 90), 15)
        self.assertAlmostEqual(ground_speed(20, 90, 5, 270), 25)

    def test_crosswind(self):
        gs = ground_speed(20, 0, 5, 90)
        self.assertAlmostEqual(gs, math.sqrt(400 - 25))
        # Wind from the east -> crab right (into wind).
        self.assertGreater(heading_for_course(20, 0, 5, 90), 0)
        self.assertLess(heading_for_course(20, 0, 5, 90), 90)

    def test_infeasible(self):
        self.assertIsNone(ground_speed(10, 0, 12, 90))
        self.assertIsNone(ground_speed(10, 90, 12, 90))

    def test_profile_interpolation(self):
        p = WindProfile([WindLayer(0, 0, 0), WindLayer(100, 10, 270)])
        s, d = p.at(50)
        self.assertAlmostEqual(s, 5)
        self.assertAlmostEqual(d, 270)
        # Wrap-around between 350 and 010 should give ~000, not 180.
        p = WindProfile([WindLayer(0, 10, 350), WindLayer(100, 10, 10)])
        s, d = p.at(50)
        self.assertTrue(d < 1 or d > 359)


class Estimator(unittest.TestCase):
    def test_straight_leg(self):
        ac = AircraftProfile(cruise_airspeed=20.0)
        m = straight_mission(10000)
        calm = estimate(m, ac)
        self.assertAlmostEqual(calm.total_time_s, 500, delta=2)
        head = estimate(m, ac, WindProfile.uniform(5, 90))
        self.assertAlmostEqual(head.total_time_s, 10000 / 15, delta=3)

    def test_round_trip_wind_always_slower(self):
        m = straight_mission(10000)
        m.items.append(M.MissionItem(2, M.NAV_RETURN_TO_LAUNCH))
        ac = AircraftProfile(rtl_altitude=-1, include_fw_landing=False)
        calm = estimate(m, ac).total_time_s
        for d in range(0, 360, 30):
            self.assertGreater(estimate(m, ac, WindProfile.uniform(6, d)).total_time_s, calm)

    def test_min_groundspeed_raises_airspeed(self):
        m = straight_mission(10000)
        ac = AircraftProfile(cruise_airspeed=15, max_airspeed=25, min_groundspeed=10)
        e = estimate(m, ac, WindProfile.uniform(10, 90))   # 10 m/s headwind
        self.assertAlmostEqual(e.segments[0].groundspeed, 10, delta=0.01)

    def test_wind_too_strong(self):
        m = straight_mission(10000)
        e = estimate(m, AircraftProfile(cruise_airspeed=15), WindProfile.uniform(20, 90))
        self.assertFalse(e.feasible)
        self.assertTrue(e.warnings)

    def test_examples_parse_and_estimate(self):
        fw = M.load_file(os.path.join(EX, "fixed_wing_survey.waypoints"))[1]
        self.assertEqual(fw.home_alt_msl, 584.0)
        e = estimate(fw, PRESETS["Generic fixed wing"])
        self.assertTrue(e.feasible)
        # DO_JUMP repeats waypoints 3..7 once -> waypoint 3 appears twice.
        self.assertEqual(sum(1 for s in e.segments if s.label == "#3 WAYPOINT"), 2)
        vt = M.load_file(os.path.join(EX, "vtol_mapping.waypoints"))[1]
        e = estimate(vt, PRESETS["Generic VTOL (QuadPlane)"])
        kinds = [s.label for s in e.segments if s.kind == "transition"]
        self.assertEqual(kinds, ["Forward transition", "Back transition"])
        self.assertEqual(e.segments[-1].alt_end, 0.0)

    def test_vtol_landing_time(self):
        ac = AircraftProfile(kind=VTOL, vtol_descent_rate=2.0, vtol_land_final_alt=6, vtol_land_final_speed=0.5,
                             back_transition_time=0)
        m = M.Mission("t", "t", 0, 0, 0, [M.MissionItem(1, M.NAV_VTOL_TAKEOFF, alt=46),
                                          M.MissionItem(2, M.NAV_VTOL_LAND)])
        e = estimate(m, ac)
        land = e.segments[-1]
        self.assertAlmostEqual(land.time_s, 40 / 2.0 + 6 / 0.5)

    def test_sweep(self):
        m = straight_mission(10000)
        sweep = direction_sweep(m, AircraftProfile(), WindProfile.uniform(5, 0), 90)
        times = dict(sweep)
        self.assertGreater(times[90], times[270])   # from east = headwind on eastbound leg


class Parsers(unittest.TestCase):
    def test_kml_and_kmz(self):
        for name in ("route.kml", "route.kmz"):
            kind, routes = M.load_file(os.path.join(EX, name))
            self.assertEqual(kind, "routes")
            self.assertEqual(len(routes[0].points), 6)
            m = M.route_to_mission(routes[0], vtol=False)
            self.assertEqual(m.items[0].command, M.NAV_TAKEOFF)
            self.assertEqual(m.items[-1].command, M.NAV_RETURN_TO_LAUNCH)
            self.assertEqual(m.items[1].alt, 120)

    def test_kml_points_and_absolute(self):
        kml = b"""<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
        <Placemark><Point><altitudeMode>absolute</altitudeMode><coordinates>10,50,500</coordinates></Point></Placemark>
        <Placemark><Point><altitudeMode>absolute</altitudeMode><coordinates>10.01,50,600</coordinates></Point></Placemark>
        <Placemark><Point><altitudeMode>absolute</altitudeMode><coordinates>10.02,50,600</coordinates></Point></Placemark>
        </Document></kml>"""
        routes = M.parse_kml(kml)
        m = M.route_to_mission(routes[0])
        self.assertEqual([i.alt for i in m.items if i.command == M.NAV_WAYPOINT], [100, 100])

    def test_qgc_plan(self):
        plan = """{"fileType":"Plan","mission":{"plannedHomePosition":[47.0,8.0,400],
          "items":[{"type":"SimpleItem","command":22,"frame":3,"params":[0,0,0,null,0,0,50]},
                   {"type":"SimpleItem","command":16,"frame":3,"params":[0,0,0,null,47.01,8.0,100]},
                   {"type":"ComplexItem","complexItemType":"survey","TransectStyleComplexItem":
                      {"CameraCalc":{"DistanceToSurface":90},"VisualTransectPoints":[[47.01,8.01],[47.02,8.01]]}},
                   {"type":"SimpleItem","command":20,"frame":2,"params":[0,0,0,0,0,0,0]}]}}"""
        m = M.parse_qgc_plan(plan)
        self.assertEqual(len(m.items), 5)
        self.assertEqual(m.items[2].alt, 90)

    def test_bad_file(self):
        with self.assertRaises(M.MissionParseError):
            M.parse_waypoints("hello world")


class Weather(unittest.TestCase):
    def test_open_meteo(self):
        data = {"elevation": 500.0, "hourly": {
            "time": ["2026-11-19T10:00", "2026-11-19T11:00"],
            "wind_speed_10m": [3, 4], "wind_direction_10m": [270, 280],
            "wind_speed_80m": [5, 6], "wind_direction_80m": [275, 285],
            "wind_speed_120m": [6, None], "wind_direction_120m": [276, None],
            "wind_speed_1000hPa": [7, 7], "wind_direction_1000hPa": [280, 280],
            "geopotential_height_1000hPa": [100, 100],
            "wind_speed_925hPa": [9, 9], "wind_direction_925hPa": [290, 290],
            "geopotential_height_925hPa": [1300, 1300]}}
        fc = parse_open_meteo(data, 1, 2)
        self.assertEqual(len(fc.times), 2)
        alts = [l.alt_agl_m for l in fc.profiles[0].layers]
        self.assertEqual(alts, [10, 80, 120, 800])     # 1000 hPa is below ground here -> skipped
        self.assertEqual(len(fc.profiles[1].layers), 3)

    def test_metar(self):
        data = [{"icaoId": "KFAR", "lat": 41, "lon": -105, "wdir": 200, "wspd": 10},
                {"icaoId": "KNEA", "lat": 40.1, "lon": -105, "wdir": "VRB", "wspd": 3}]
        w = parse_metars(data, 40, -105)
        self.assertEqual(w.station, "KNEA")
        self.assertTrue(w.variable)
        self.assertAlmostEqual(w.speed_ms, 3 * 0.514444)


class BaseMap(unittest.TestCase):
    def test_mercator_round_trip(self):
        from flighttime import basemap
        for lat, lon, z in ((40.585, -105.08, 12), (-35.36, 149.17, 15), (0, 0, 3)):
            x, y = basemap.world_px(lat, lon, z)
            lat2, lon2 = basemap.latlon_from_px(x, y, z)
            self.assertAlmostEqual(lat, lat2, places=6)
            self.assertAlmostEqual(lon, lon2, places=6)
        # Tile 0/0/0 covers the whole world: lon 0 at the centre.
        self.assertAlmostEqual(basemap.world_px(0, 0, 0)[0], 128)

    def test_fit_zoom(self):
        from flighttime import basemap
        z = basemap.fit_zoom((40.58, -105.08, 40.68, -104.95), 800, 400)
        x0, y0 = basemap.world_px(40.68, -105.08, z)
        x1, y1 = basemap.world_px(40.58, -104.95, z)
        self.assertLessEqual(x1 - x0, 700)
        self.assertGreater(2 * (x1 - x0), 700 - 1e-6 if 2 * (y1 - y0) <= 300 else 0)


if __name__ == "__main__":
    unittest.main()
