"""Build a standalone executable for the current operating system with PyInstaller.

    pip install -r requirements-build.txt
    python build.py

Output goes to ``dist/``:
  * Windows: dist/FlightTimeCalc.exe
  * macOS:   dist/FlightTimeCalc.app (zipped to FlightTimeCalc-macos.zip)
  * Linux:   dist/FlightTimeCalc
"""

import os
import platform
import shutil
import subprocess
import sys

NAME = "FlightTimeCalc"
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    system = platform.system()
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", NAME,
            "--windowed", "--hidden-import", "certifi", "run_flighttime.py"]
    # A macOS .app must be a folder bundle; Windows and Linux get a single file.
    args.insert(3, "--onedir" if system == "Darwin" else "--onefile")
    if system == "Darwin":
        args[3:3] = ["--osx-bundle-identifier", "io.github.flighttimecalc"]
    print(" ".join(args))
    subprocess.check_call(args, cwd=HERE)
    dist = os.path.join(HERE, "dist")
    if system == "Darwin":
        shutil.make_archive(os.path.join(dist, NAME + "-macos"), "zip", dist, NAME + ".app")
    print("\nBuilt into", dist)
    for f in sorted(os.listdir(dist)):
        print("  ", f)


if __name__ == "__main__":
    main()
