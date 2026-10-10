"""Estimate print time and filament for every STL with real slicer CLIs.

Flattens each slicer's bundled vendor presets (no GUI setup needed), slices
every STL with a 0.4 mm nozzle, 0.20 mm layers, 2 walls and 15 % infill, and
prints time and grams. ``deck_leg`` is sliced as the four-up job it is printed
as. Run from the repo root::

    python3 hardware/print/slice.py              # every profile below
    python3 hardware/print/slice.py --profile "A1 PETG 0.20"

Profiles:

* ``A1 ...``   Bambu Lab A1 stock presets via OrcaSlicer
  (``brew install --cask orcaslicer``). Reports model print time; the printer
  adds about 6 min of bed levelling and calibration on top.
* ``MK4S ...`` Prusa MK4S stock presets via PrusaSlicer
  (``brew install --cask prusaslicer``).
* ``MK3S ...`` Prusa MK3S presets: a slow, no-input-shaper printer for the
  worst case.
"""

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRUSA = Path("/Applications/PrusaSlicer.app/Contents")
ORCA = Path("/Applications/OrcaSlicer.app/Contents")
COPIES = {"deck_leg": 4}  # parts printed as several copies in one job

PROFILES = {
    "A1 PETG 0.20": ("orca", "Bambu Lab A1 0.4 nozzle", "0.20mm Standard @BBL A1", "Generic PETG @BBL A1"),
    "A1 PLA 0.20": ("orca", "Bambu Lab A1 0.4 nozzle", "0.20mm Standard @BBL A1", "Generic PLA @BBL A1"),
    "MK4S PETG 0.20": ("prusa", "Original Prusa MK4S 0.4 nozzle", "0.20mm SPEED @MK4S 0.4", "Generic PETG @MK4S"),
    "MK4S PLA 0.20": ("prusa", "Original Prusa MK4S 0.4 nozzle", "0.20mm SPEED @MK4S 0.4", "Generic PLA @MK4S"),
    "MK3S PETG 0.20": ("prusa", "Original Prusa i3 MK3S & MK3S+", "0.20mm SPEED @MK3", "Generic PETG"),
}
PRUSA_DROP = {
    "compatible_printers", "compatible_printers_condition", "compatible_prints",
    "compatible_prints_condition", "renamed_from", "alias", "printer_model",
    "printer_variant", "default_print_profile", "default_filament_profile",
}


# --- PrusaSlicer -------------------------------------------------------------
def prusa_bundle():
    secs, cur = {}, None
    for line in (PRUSA / "Resources/profiles/PrusaResearch.ini").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            cur = secs.setdefault(m.group(1), {})
        elif cur is not None and not line.startswith("#") and " =" in line:
            k, _, v = line.partition(" =")
            cur[k.strip()] = v.lstrip()
    return secs


def prusa_resolve(secs, name):
    kind = name.split(":", 1)[0]
    own, out = secs[name], {}
    for parent in filter(None, (p.strip() for p in own.get("inherits", "").split(";"))):
        out.update(prusa_resolve(secs, f"{kind}:{parent}"))
    out.update({k: v for k, v in own.items() if k != "inherits"})
    return out


def prusa_slice(tmp, printer, process, filament, stl, copies):
    secs = prusa_bundle()
    cfg = {}
    for name in (f"printer:{printer}", f"print:{process}", f"filament:{filament}"):
        cfg.update(prusa_resolve(secs, name))
    cfg = {k: v for k, v in cfg.items() if k not in PRUSA_DROP}
    cfg.update(fill_density="15%", perimeters="2", binary_gcode="0")
    ini, out = tmp / "profile.ini", tmp / "out.gcode"
    ini.write_text("".join(f"{k} = {v}\n" for k, v in cfg.items()))
    place = ["--duplicate", str(copies)] if copies > 1 else ["--center", "110,105"]
    subprocess.run([str(PRUSA / "MacOS/PrusaSlicer"), "--load", str(ini), *place, "-g", str(stl), "-o", str(out)],
                   check=True, capture_output=True)
    g = out.read_text(errors="ignore")
    return _grab(g, r"estimated printing time \(normal mode\) = (.+)"), _grab(g, r"total filament used \[g\] = (.+)")


# --- OrcaSlicer (Bambu presets) ---------------------------------------------
def orca_resolve(kind, name):
    data = json.loads((ORCA / "Resources/profiles/BBL" / kind / f"{name}.json").read_text())
    if "inherits" in data:
        base = orca_resolve(kind, data.pop("inherits"))
        base.update(data)
        data = base
    return data


def orca_slice(tmp, printer, process, filament, stl, copies):
    files = []
    for kind, name, extra in (
        ("machine", printer, {"curr_bed_type": "Textured PEI Plate"}),
        ("process", process, {"curr_bed_type": "Textured PEI Plate", "sparse_infill_density": "15%", "wall_loops": "2"}),
        ("filament", filament, {}),
    ):
        cfg = orca_resolve(kind, name) | extra | {"name": name}
        path = tmp / f"{kind}.json"
        path.write_text(json.dumps(cfg))
        files.append(path)
    out = tmp / "orca"
    subprocess.run([str(ORCA / "MacOS/OrcaSlicer"), "--arrange", "1", "--slice", "0",
                    "--load-settings", f"{files[0]};{files[1]}", "--load-filaments", str(files[2]),
                    "--outputdir", str(out), *[str(stl)] * copies], check=True, capture_output=True, cwd=tmp)
    g = (out / "plate_1.gcode").read_text(errors="ignore")
    return _grab(g, r"model printing time: ([^;]+)"), _grab(g, r"filament used \[g\] = (.+)")


def _grab(text, pattern):
    m = re.search(pattern, text)
    return m.group(1).strip() if m else "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=PROFILES, action="append")
    args = ap.parse_args()
    for label in args.profile or PROFILES:
        tool, *preset = PROFILES[label]
        app = ORCA if tool == "orca" else PRUSA
        if not app.exists():
            print(f"{label}: skipped, {app.parent.name} not installed")
            continue
        for stl in sorted((HERE / "stl").glob("*.stl")):
            copies = COPIES.get(stl.stem, 1)
            with tempfile.TemporaryDirectory() as tmp:
                fn = orca_slice if tool == "orca" else prusa_slice
                time, grams = fn(Path(tmp), *preset, stl, copies)
            name = f"{stl.stem} x{copies}" if copies > 1 else stl.stem
            print(f"{label:15s} {name:16s} {time:>11s} {grams:>6s} g")


if __name__ == "__main__":
    main()
