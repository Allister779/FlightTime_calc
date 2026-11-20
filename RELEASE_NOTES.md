Estimate how long a fixed-wing or VTOL (QuadPlane) mission will take, with zero wind and with
the winds you set or fetch live.

## Download

| System | File | How to start |
|---|---|---|
| Windows 10/11 | `FlightTimeCalc-windows-x64.exe` | Double-click. If SmartScreen warns, choose **More info → Run anyway**. |
| macOS, Apple Silicon (M1 and later) | `FlightTimeCalc-macos-apple-silicon.zip` | Unzip, then right-click `FlightTimeCalc.app` → **Open** the first time. |
| macOS, Intel | `FlightTimeCalc-macos-intel.zip` | Unzip, then right-click `FlightTimeCalc.app` → **Open** the first time. |
| Linux x86-64 | `FlightTimeCalc-linux-x86_64` | `chmod +x FlightTimeCalc-linux-x86_64`, then run it. |

No installation or Python needed. The apps are not code-signed, which is why Windows and macOS
ask for confirmation the first time.

## Features

- Opens Mission Planner `.waypoints`, QGroundControl `.plan`, and Google Earth KML/KMZ flight plans.
- Simulates take-off, waypoints, loiters, DO_JUMP loops, speed changes, return-to-launch, landing,
  and VTOL take-off, transitions and landing.
- Wind entered by hand (one wind, or different winds by height), or fetched live:
  Open-Meteo forecast winds aloft (worldwide) or the nearest airport METAR (NOAA).
- Total time with zero wind and with wind, best and worst wind direction, and an endurance check.
- Route map on OpenStreetMap or OpenTopoMap, with legs coloured by whether the wind slows or
  speeds them; per-leg table exportable to CSV.
- Metric units by default; light, dark or system appearance; readable at any display scaling.

These are planning estimates. Turns, gusts and battery behaviour also affect real flight time,
so keep a reserve.
