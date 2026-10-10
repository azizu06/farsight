"""Verdict and report logic for jetson/nvblox-smoke.sh: parses what the script sampled and decides PASS/FAIL.

The shell script launches nvblox and collects raw samples into a directory (tegrastats, /proc/meminfo, process
RSS, `ros2 topic hz` / `delay` output, startup and parameter checks); this module turns them into gates, so
every threshold and parser is unit-tested off the Jetson (jetson/tests/test_nvblox_smoke.py).

    python3 -m jetson.nvblox_smoke param-keys ros2/farsight_nvblox/config/nvblox.yaml
    python3 -m jetson.nvblox_smoke report --samples DIR --out report.md [--min-slice-hz 5 ...]
"""

import argparse
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

TEGRASTATS_RAM = re.compile(r"RAM (\d+)/(\d+)MB")
TEGRASTATS_SWAP = re.compile(r"SWAP (\d+)/(\d+)MB")
TEGRASTATS_GPU = re.compile(r"GR3D_FREQ (\d+)%")
TEGRASTATS_POWER = re.compile(r"VDD_IN (\d+)mW")
HZ = re.compile(r"average rate:\s*([0-9.]+)")
DELAY = re.compile(r"average delay:\s*(-?[0-9.]+)")
DELAY_MAX = re.compile(r"max:\s*(-?[0-9.]+)")
CUDA_FAILURE = re.compile(r"cuda(?:error|\s+error)|out of memory|illegal memory|no kernel image|"
                          r"CUDA_ERROR|failed to (?:initialize|create) .*(?:cuda|vpi|nitros)", re.I)


@dataclass
class Thresholds:
    min_slice_hz: float = 5.0         # 2D ESDF slice, the costmap's input; update_esdf_rate_hz is 10
    min_mesh_hz: float = 0.5          # only judged when the mesh was enabled
    min_available_mb: float = 1000.0  # MemAvailable floor for the whole run (report gate 3)
    max_latency_ms: float = 300.0     # depth stamp to slice, mean (report gate 4)
    max_gpu_pct: float = 90.0         # mean GR3D_FREQ; a saturated GPU starves YOLO and the depth engine
    min_costmap_hz: float = 2.0       # Nav2 local costmap, only with --nav2
    max_startup_s: float = 90.0


@dataclass
class Gate:
    name: str
    status: str   # PASS, FAIL, WARN (not decisive), SKIP
    detail: str


def parse_tegrastats(text):
    rows = []
    for line in text.splitlines():
        ram = TEGRASTATS_RAM.search(line)
        if not ram:
            continue
        swap, gpu, power = (p.search(line) for p in (TEGRASTATS_SWAP, TEGRASTATS_GPU, TEGRASTATS_POWER))
        rows.append(dict(ram_used=int(ram[1]), ram_total=int(ram[2]),
                         swap_used=int(swap[1]) if swap else 0,
                         gpu_pct=int(gpu[1]) if gpu else None,
                         power_mw=int(power[1]) if power else None))
    return rows


def parse_rates(text):
    return [float(m[1]) for m in HZ.finditer(text)]


def parse_delays(text):
    """(mean seconds per window, worst max seconds) from `ros2 topic delay` output."""
    means = [float(m[1]) for m in DELAY.finditer(text)]
    maxima = [float(m[1]) for m in DELAY_MAX.finditer(text)]
    return means, maxima


def parse_series(text):
    """'epoch,value' lines -> list of floats (value column)."""
    values = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2:
            try:
                values.append(float(parts[1]))
            except ValueError:
                pass
    return values


def parse_meta(text):
    meta = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            meta[key.strip()] = value.strip()
    return meta


def flatten_param_keys(params):
    """ROS parameter file -> dotted parameter names, as `ros2 param get` knows them."""
    def walk(prefix, node):
        for key, value in node.items():
            if isinstance(value, dict):
                yield from walk(f"{prefix}{key}.", value)
            else:
                yield f"{prefix}{key}"
    keys = []
    for body in params.values():
        keys.extend(walk("", body.get("ros__parameters", {})))
    return sorted(set(keys))


def read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def rate_gate(name, text, minimum, *, required=True):
    rates = parse_rates(text)
    if not rates:
        return Gate(name, "FAIL" if required else "SKIP", "no `ros2 topic hz` samples: topic never published")
    median, worst = statistics.median(rates), min(rates)
    status = "PASS" if median >= minimum else "FAIL"
    return Gate(name, status, f"median {median:.2f} Hz, worst window {worst:.2f} Hz, need >= {minimum:g} Hz "
                              f"({len(rates)} windows)")


def evaluate(samples, thresholds=None):
    """samples: directory the shell script filled. Returns (verdict, gates, facts)."""
    t, d = thresholds or Thresholds(), Path(samples)
    meta = parse_meta(read(d / "meta.env"))
    gates = []

    startup = meta.get("startup_s", "")
    if startup in ("", "timeout"):
        gates.append(Gate("startup", "FAIL", f"nvblox_node did not appear within {t.max_startup_s:g} s"))
    elif float(startup) > t.max_startup_s:
        gates.append(Gate("startup", "FAIL", f"node up after {startup} s, limit {t.max_startup_s:g} s"))
    else:
        gates.append(Gate("startup", "PASS", f"nvblox_node up after {startup} s"))

    errors = [line for line in read(d / "launch.log").splitlines() if CUDA_FAILURE.search(line)]
    gates.append(Gate("cuda errors", "FAIL" if errors else "PASS",
                      f"{len(errors)} CUDA/VPI failure line(s), first: {errors[0].strip()[:160]}" if errors
                      else "no CUDA/VPI failure in the launch log"))

    missing = [line.strip() for line in read(d / "params_missing.txt").splitlines() if line.strip()]
    checked = meta.get("params_checked", "0")
    gates.append(Gate("parameters", "FAIL" if missing or checked == "0" else "PASS",
                      f"{len(missing)} of {checked} configured parameters not declared by nvblox_node: "
                      f"{', '.join(missing[:6])}" if missing else f"all {checked} configured parameters declared"))

    gates.append(rate_gate("esdf slice rate", read(d / "slice_hz.txt"), t.min_slice_hz))

    mesh_hz = float(meta.get("mesh_hz", "0") or 0)
    if mesh_hz > 0:
        gates.append(rate_gate("mesh rate", read(d / "mesh_hz.txt"), min(t.min_mesh_hz, mesh_hz * 0.5)))
    else:
        gates.append(Gate("mesh rate", "SKIP", "mesh disabled (mesh_rate_hz 0); re-run with --mesh-hz to cost it"))

    means, maxima = parse_delays(read(d / "slice_delay.txt"))
    if means:
        mean_ms, worst_ms = statistics.mean(means) * 1000, (max(maxima) * 1000 if maxima else max(means) * 1000)
        gates.append(Gate("slice latency", "PASS" if mean_ms <= t.max_latency_ms else "FAIL",
                          f"mean {mean_ms:.0f} ms, worst {worst_ms:.0f} ms, need mean <= {t.max_latency_ms:g} ms"))
    else:
        gates.append(Gate("slice latency", "WARN", "`ros2 topic delay` printed nothing (slice message may lack a "
                                                    "header stamp); measure depth-to-costmap delay by hand"))

    available = [kb / 1024 for kb in parse_series(read(d / "meminfo.csv"))]
    tegra = parse_tegrastats(read(d / "tegrastats.txt"))
    if available:
        low, source = min(available), "MemAvailable"
    elif tegra:
        low, source = min(r["ram_total"] - r["ram_used"] for r in tegra), "tegrastats total-used"
    else:
        low = source = None
    if low is None:
        gates.append(Gate("memory", "FAIL", "no memory samples"))
    else:
        gates.append(Gate("memory", "PASS" if low >= t.min_available_mb else "FAIL",
                          f"lowest {source} {low:.0f} MB, need >= {t.min_available_mb:g} MB"))

    gpu = [r["gpu_pct"] for r in tegra if r["gpu_pct"] is not None]
    if gpu:
        mean_gpu = statistics.mean(gpu)
        gates.append(Gate("gpu", "PASS" if mean_gpu <= t.max_gpu_pct else "FAIL",
                          f"mean GR3D_FREQ {mean_gpu:.0f}% (peak {max(gpu)}%), need mean <= {t.max_gpu_pct:g}%"))
    else:
        gates.append(Gate("gpu", "FAIL", "no GR3D_FREQ samples in tegrastats output (tegrastats may need sudo on this image)"))

    if meta.get("nav2") == "1":
        gates.append(rate_gate("costmap update rate", read(d / "costmap_hz.txt"), t.min_costmap_hz))
    else:
        gates.append(Gate("costmap update rate", "SKIP", "Nav2 not running (--nav2 not given); slice rate stands in"))

    rss = [kb / 1024 for kb in parse_series(read(d / "rss.csv"))]
    swap = max((r["swap_used"] for r in tegra), default=0)
    power = [r["power_mw"] for r in tegra if r["power_mw"] is not None]
    facts = dict(
        peak_rss_mb=max(rss) if rss else None,
        peak_swap_mb=swap,
        ram_total_mb=tegra[0]["ram_total"] if tegra else None,
        mean_power_mw=statistics.mean(power) if power else None,
        peak_power_mw=max(power) if power else None,
        meta=meta)
    if swap > 0:
        gates.append(Gate("swap", "WARN", f"swap in use ({swap} MB peak): memory pressure even if the floor held"))
    verdict = "FAIL" if any(g.status == "FAIL" for g in gates) else "PASS"
    return verdict, gates, facts


def render_report(verdict, gates, facts):
    meta = facts["meta"]
    lines = [f"# nvblox smoke test: {verdict}", "",
             "Software-on-hardware evidence only when run on the Jetson; a run on any other machine proves nothing.", ""]
    for key in ("date", "host", "tegra_release", "cuda", "power_mode", "source", "duration_s", "voxel_size",
                "slice_heights", "mesh_hz", "nav2", "server_running", "load_notes", "nvblox_packages"):
        if key in meta:
            lines.append(f"- {key.replace('_', ' ')}: {meta[key]}")
    lines += ["", "| Gate | Result | Detail |", "| --- | --- | --- |"]
    lines += [f"| {g.name} | {g.status} | {g.detail} |" for g in gates]
    lines.append("")
    if facts["peak_rss_mb"] is not None:
        lines.append(f"- peak nvblox container RSS: {facts['peak_rss_mb']:.0f} MB")
    lines.append(f"- peak swap: {facts['peak_swap_mb']} MB")
    if facts["mean_power_mw"] is not None:
        lines.append(f"- VDD_IN mean {facts['mean_power_mw']:.0f} mW, peak {facts['peak_power_mw']} mW")
    fails = [g for g in gates if g.status == "FAIL"]
    lines += ["", f"Verdict: **{verdict}**" + ("" if not fails else " (" + ", ".join(g.name for g in fails) + ")")]
    if verdict == "PASS":
        lines.append("Go: promote `costmap_source:=nvblox` only after the live-phone and Nav2 gates in docs/nvblox.md; "
                     "keep `costmap_source:=pointcloud` as the fallback.")
    else:
        lines.append("No-go: ship `costmap_source:=pointcloud` and stop spending time on nvblox (docs/nvblox.md).")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    keys = sub.add_parser("param-keys", help="print the dotted parameter names a ROS parameter file sets")
    keys.add_argument("params")
    report = sub.add_parser("report", help="evaluate a samples directory and write the report")
    report.add_argument("--samples", required=True)
    report.add_argument("--out", required=True)
    for field_ in Thresholds.__dataclass_fields__.values():
        report.add_argument("--" + field_.name.replace("_", "-"), type=float, default=field_.default)
    args = parser.parse_args(argv)
    if args.command == "param-keys":
        import yaml
        print("\n".join(flatten_param_keys(yaml.safe_load(Path(args.params).read_text()))))
        return 0
    thresholds = Thresholds(**{name: getattr(args, name) for name in Thresholds.__dataclass_fields__})
    verdict, gates, facts = evaluate(args.samples, thresholds)
    text = render_report(verdict, gates, facts)
    Path(args.out).write_text(text)
    print(text)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
