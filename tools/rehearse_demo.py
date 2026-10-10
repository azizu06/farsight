"""Passive real-rig rehearsal log and go/no-go report (issue #42).

python -m tools.rehearse_demo record --url https://farsight.local:8443 --relay http://127.0.0.1:8080 --out runs-rehearsal/r1
python -m tools.rehearse_demo mark   --dir runs-rehearsal/r1 motor_direction pass --by Sai --note "both wheels forward"
python -m tools.rehearse_demo report --dir runs-rehearsal/r1

Passive: only GET of /health, /control/status, /camera.jpg (headers only) on the
selected upstream and /relay/health on the relay. It never opens /ws/control,
sends a motor command, takes controller ownership or touches the network. Run it
while the Quest drives. Optional --ssh reads thermal zones and load average
over an explicitly named target (BatchMode: no password prompts, no sudo).

Every check is pass, fail or unknown. Software checks come from the samples;
physical checks stay unknown until a human runs `mark`. A report is GO only when
every check passes, so a run with missing physical checks, a simulated source or
stale samples can never be GO. Permits and session tokens are never stored.
"""

import argparse
import html
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .netutil import percentile, tls_context

ALLOWED_UPSTREAM = ("/health", "/control/status", "/camera.jpg")
ALLOWED_RELAY = ("/relay/health",)
SSH_COMMAND = "cat /proc/loadavg; for z in /sys/class/thermal/thermal_zone*; do echo \"$(cat $z/type) $(cat $z/temp)\"; done"
HEALTH_KEYS = ("source", "camera", "camera_age_ms", "camera_source", "camera_error", "detector", "detector_backend",
               "detector_fps", "detector_inference_ms", "detector_error", "rover", "estop", "replay", "tracking",
               "age_ms")
CONTROL_KEYS = ("permit_ms", "estop", "mode", "link_ms", "rover", "simulated", "requires_release", "reason",
                "left_pwm", "right_pwm", "max_pwm", "min_pwm", "range_cm")
VIDEO_KEYS = ("enabled", "ready", "error", "fps", "bitrate")
STATES = ("pass", "fail", "unknown")

# Physical checks: only a human can establish them. (id, what to do / observe)
PHYSICAL = (
    ("cold_startup", "Cold boot: power on to READY and VR video in under 2 min (stopwatch)"),
    ("phone_disconnect", "Unplug each phone in turn; rover stops and recovers when replugged"),
    ("quest_disconnect", "Unplug Quest USB; rover stops within the 250 ms lease"),
    ("camera_disconnect", "Unplug the camera; motion disabled on stale video, recovers on replug"),
    ("encoder_recovery", "Kill/restart the H.264 encoder; video returns without restarting the rover"),
    ("held_trigger_stays_stopped", "Original held trigger stays stopped after a fault"),
    ("release_repress", "Explicit release then re-press resumes motion"),
    ("power_under_load", "Battery/Jetson power holds for the full 15 min loaded run (no brownout)"),
    ("temperature", "Jetson and motor driver temperatures acceptable after 15 min"),
    ("motor_direction", "Each wheel spins the commanded direction"),
    ("starting_pwm", "Starting PWM overcomes stiction on the floor"),
    ("uno_watchdog", "Uno stops on its independent 300 ms watchdog with the Jetson killed"),
    ("displayed_video_age", "Browser/headset displayed video age measured with a physical timer"),
)


def now_iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- passive collection

def passive_get(base, path, allowed, context=None, timeout=2, headers_only=False):
    """The only network read in this tool; refuses any path outside the allowlist."""
    if path not in allowed:
        raise ValueError(f"{path} is not a passive rehearsal endpoint")
    request = urllib.request.Request(base.rstrip("/") + path, method="GET")
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        if headers_only:
            return dict(response.headers)
        return json.loads(response.read(1 << 20))


def pick(source, keys):
    return {key: source[key] for key in keys if isinstance(source, dict) and key in source}


def collect_sample(url, relay=None, context=None, ssh=None, fetch=passive_get, ssh_run=None):
    """One timestamped sample; any failure becomes an `errors` entry and leaves the value absent (unknown)."""
    sample = dict(t=round(time.time(), 3), errors=[])
    started = time.monotonic()
    try:
        health = fetch(url, "/health", ALLOWED_UPSTREAM, context)
        if not isinstance(health, dict):
            raise ValueError("health is not an object")
        sample["rtt_ms"] = round((time.monotonic() - started) * 1000, 1)
        sample["health"] = pick(health, HEALTH_KEYS)
        sample["video"] = pick(health.get("video"), VIDEO_KEYS)
        sample["control"] = pick(health.get("control"), CONTROL_KEYS)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        sample["errors"].append(f"health: {type(exc).__name__}")
    try:
        status = fetch(url, "/control/status", ALLOWED_UPSTREAM, context)
        if isinstance(status, dict):
            sample["control"] = {**sample.get("control", {}), **pick(status, CONTROL_KEYS)}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        sample["errors"].append(f"control: {type(exc).__name__}")
    try:
        headers = fetch(url, "/camera.jpg", ALLOWED_UPSTREAM, context, headers_only=True)
        if not isinstance(headers, dict):
            raise ValueError("headers are not a mapping")
        age = headers.get("X-Frame-Age-Ms")
        sample["camera_jpg"] = dict(age_ms=float(age) if age is not None else None,
                                    source=headers.get("X-Frame-Source"), frame=headers.get("X-Frame-Id"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        sample["errors"].append(f"camera.jpg: {type(exc).__name__}")
    if relay:
        try:
            sample["relay"] = pick(fetch(relay, "/relay/health", ALLOWED_RELAY, None), ("relay", "quest_built"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            sample["errors"].append(f"relay: {type(exc).__name__}")
    if ssh:
        sample["jetson"] = (ssh_run or ssh_read)(ssh)
    return sample


def ssh_read(target, timeout=6):
    """Read-only thermal/load read on an explicitly supplied target. BatchMode: never prompts for a password."""
    try:
        done = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", target, SSH_COMMAND],
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return dict(error=type(exc).__name__)
    if done.returncode != 0:
        return dict(error=f"ssh exit {done.returncode}")
    return parse_ssh(done.stdout)


def parse_ssh(text):
    lines = [line.split() for line in text.strip().splitlines()]
    result = {}
    try:
        result["load1"] = float(lines[0][0])
    except (IndexError, ValueError):
        pass
    temps = {}
    for parts in lines[1:]:
        try:
            temps[parts[0]] = int(parts[-1]) / 1000
        except (IndexError, ValueError):
            continue
    if temps:
        result["temp_c_max"] = max(temps.values())
    return result or dict(error="unparseable")


# --------------------------------------------------------------------------- bounded log

class BoundedLog:
    """JSONL with size rotation: at most `keep` files of `max_bytes`; the oldest is dropped and counted."""

    def __init__(self, directory, max_bytes=2_000_000, keep=3):
        self.dir, self.max_bytes, self.keep = Path(directory), max_bytes, keep
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "meta.json"
        self.meta = self._load_meta()
        self.handle = open(self.dir / "samples.jsonl", "a")

    def _load_meta(self):
        try:
            meta = json.loads(self.meta_path.read_text())
            return meta if isinstance(meta, dict) else {}
        except (OSError, ValueError):
            return {}

    def write(self, sample):
        if self.handle.tell() >= self.max_bytes:
            self._rotate()
        self.handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
        self.handle.flush()
        self.meta["samples"] = self.meta.get("samples", 0) + 1
        self.meta.setdefault("started", sample["t"])
        self.meta["ended"] = sample["t"]

    def _rotate(self):
        self.handle.close()
        oldest = self.dir / f"samples.jsonl.{self.keep - 1}"
        if oldest.exists():
            self.meta["rotated_out_bytes"] = self.meta.get("rotated_out_bytes", 0) + oldest.stat().st_size
            oldest.unlink()
        for index in range(self.keep - 2, 0, -1):
            source = self.dir / f"samples.jsonl.{index}"
            if source.exists():
                source.rename(self.dir / f"samples.jsonl.{index + 1}")
        (self.dir / "samples.jsonl").rename(self.dir / "samples.jsonl.1")
        self.handle = open(self.dir / "samples.jsonl", "a")
        self.save_meta()

    def save_meta(self):
        self.meta_path.write_text(json.dumps(self.meta))

    def close(self):
        self.save_meta()
        self.handle.close()


def read_samples(directory):
    """Retained samples oldest first. Malformed lines (including a torn last line) are counted, not fatal."""
    directory = Path(directory)
    files = sorted(directory.glob("samples.jsonl.*"), key=lambda p: -int(p.suffix[1:])) + [directory / "samples.jsonl"]
    samples, bad = [], 0
    for path in files:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
                if not isinstance(item, dict) or type(item.get("t")) not in (int, float):
                    raise ValueError
                samples.append(item)
            except ValueError:
                bad += 1
    samples.sort(key=lambda s: s["t"])
    return samples, bad


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- evaluation

def check(cid, state, title, evidence, at=None, kind="software"):
    assert state in STATES
    return dict(id=cid, kind=kind, state=state, title=title, evidence=evidence, at=now_iso(at) if at else None)


def numbers(samples, getter):
    values = []
    for sample in samples:
        try:
            value = getter(sample)
        except (KeyError, TypeError, AttributeError):
            continue
        if type(value) in (int, float) and value == value:
            values.append(value)
    return values


def stat(values):
    return f"n={len(values)} p50={percentile(values, .5):.0f} p95={percentile(values, .95):.0f} max={max(values):.0f}"


def evaluate(samples, meta, marks, now=None, min_minutes=15, max_stale_s=15, max_camera_age_ms=500, max_link_ms=200,
             min_detector_fps=10, max_temp_c=80, bad_lines=0):
    """Return (checks, summary). Missing data is unknown; nothing here ever infers a physical result."""
    now = time.time() if now is None else now
    checks = []
    if not samples:
        checks.append(check("samples", "unknown", "Samples recorded", "no readable samples"))
        samples = []
    last = samples[-1] if samples else None
    first_t, last_t = (samples[0]["t"], last["t"]) if samples else (None, None)
    duration = (last_t - first_t) if samples else 0
    ok = [s for s in samples if "health" in s]
    if samples:
        checks.append(check("reachable", "pass" if len(ok) == len(samples) else ("fail" if len(ok) < 0.95 * len(samples) else "unknown"),
                            "Upstream /health reachable for the whole run",
                            f"{len(ok)}/{len(samples)} samples answered; {bad_lines} malformed log lines", last_t))
        gaps = [b["t"] - a["t"] for a, b in zip(samples, samples[1:])]
        biggest = max(gaps) if gaps else 0
        checks.append(check("coverage", "pass" if biggest <= max_stale_s else "fail", "Sampling had no long gaps",
                            f"largest gap {biggest:.1f}s (limit {max_stale_s}s), {len(samples)} samples", last_t))
        answered = (ok[-1]["t"] - ok[0]["t"]) if ok else 0
        checks.append(check("duration", "pass" if answered >= min_minutes * 60 else "fail",
                            f"Upstream answered for at least {min_minutes} min",
                            f"answered over {answered / 60:.1f} min of {duration / 60:.1f} min recorded", last_t))
        age = now - last_t
        checks.append(check("fresh", "pass" if age <= max_stale_s else "unknown", "Evidence is current",
                            f"last sample {age:.0f}s ago (run ended or logger stopped when older than {max_stale_s}s)", last_t))
    sources = {str(s["health"].get("source") or s["health"].get("camera_source")) for s in ok}
    replay = any(s["health"].get("replay") for s in ok)
    simulated = any((s.get("control") or {}).get("simulated") for s in samples) or any("SIMULATED" in x.upper() for x in sources)
    if ok:
        real = not simulated and not replay
        checks.append(check("real_rig", "pass" if real else "fail", "Samples come from the real rig, not simulation or replay",
                            f"sources={sorted(sources)} simulated={simulated} replay={replay}", last_t))
    cam = numbers(ok, lambda s: s["health"]["camera_age_ms"])
    if ok:
        fresh = [s for s in ok if s["health"].get("camera")]
        checks.append(check("camera_fresh", "pass" if cam and len(fresh) == len(ok) and percentile(cam, .95) <= max_camera_age_ms else "fail" if cam else "unknown",
                            f"Server capture age p95 <= {max_camera_age_ms} ms (server-side only)",
                            stat(cam) + f" ms; camera fresh in {len(fresh)}/{len(ok)}" if cam else "no camera_age_ms reported", last_t))
    fps = numbers(ok, lambda s: s["health"]["detector_fps"])
    if ok:
        backend = sorted({str(s["health"].get("detector_backend")) for s in ok})
        checks.append(check("inference", "pass" if fps and percentile(fps, .05) >= min_detector_fps else "fail" if fps else "unknown",
                            f"Inference rate p5 >= {min_detector_fps} fps", f"backend {backend}; fps {stat(fps)}" if fps else f"backend {backend}; no fps", last_t))
        errors = sorted({str(s["health"]["detector_error"]) for s in ok if s["health"].get("detector_error")})
        checks.append(check("detector_clean", "fail" if errors else "pass", "Detector reported no error", "; ".join(errors) or "none seen", last_t))
    vids = [s["video"] for s in samples if s.get("video")]
    if vids:
        enabled = [v for v in vids if v.get("enabled")]
        verr = sorted({str(v["error"]) for v in vids if v.get("error")})
        state = "unknown" if not enabled else "fail" if verr else "pass" if all(v.get("ready") for v in enabled) else "fail"
        checks.append(check("video", state, "H.264 encoder ready without error",
                            "H.264 disabled (MJPEG only)" if not enabled else f"ready in {sum(bool(v.get('ready')) for v in enabled)}/{len(enabled)}; errors {verr or 'none'}", last_t))
    ctl = [s["control"] for s in samples if s.get("control")]
    if ctl:
        leases = {c.get("permit_ms") for c in ctl}
        checks.append(check("lease", "pass" if leases == {250} else "fail", "Stop lease is 250 ms", f"permit_ms seen {sorted(map(str, leases))}", last_t))
        rovers = [bool(c.get("rover")) for c in ctl]
        checks.append(check("rover_link", "pass" if all(rovers) else "fail", "Uno reported connected throughout",
                            f"rover true in {sum(rovers)}/{len(rovers)} samples", last_t))
        link = numbers(ctl, lambda c: c["link_ms"])
        checks.append(check("relay_ack", "pass" if link and percentile(link, .95) <= max_link_ms else "fail" if link else "unknown",
                            f"Relay acknowledgement latency p95 <= {max_link_ms} ms",
                            stat(link) + " ms (server link_ms while the Quest drove)" if link else "no link_ms: Quest never drove during the run", last_t))
    if any("relay" in s or any(e.startswith("relay") for e in s["errors"]) for s in samples):
        relay_ok = [s for s in samples if s.get("relay", {}).get("relay")]
        total = [s for s in samples if "relay" in s or any(e.startswith("relay") for e in s["errors"])]
        checks.append(check("relay_up", "pass" if len(relay_ok) == len(total) else "fail", "Operator relay reachable",
                            f"{len(relay_ok)}/{len(total)} relay probes answered", last_t))
    jetson = [s["jetson"] for s in samples if s.get("jetson")]
    if jetson:
        temps = numbers(jetson, lambda j: j["temp_c_max"])
        checks.append(check("jetson_temp", "unknown" if not temps else "pass" if max(temps) <= max_temp_c else "fail",
                            f"Jetson max thermal zone <= {max_temp_c} C (software read; power is separate)",
                            f"max {max(temps):.1f} C, last {temps[-1]:.1f} C" if temps else "ssh read gave no temperature", last_t))
    for cid, title in PHYSICAL:
        mark = marks.get(cid) or {}
        state = mark.get("state") if mark.get("state") in STATES else "unknown"
        checks.append(check(cid, state, title,
                            f"{mark['state']} by {mark.get('by', '?')}: {mark.get('note', '')}".strip() if mark else "not yet marked by a human",
                            mark.get("at_epoch"), kind="physical"))
    counts = {s: sum(c["state"] == s for c in checks) for s in STATES}
    verdict = "NO-GO" if counts["fail"] else "INCOMPLETE" if counts["unknown"] else "GO"
    pwm = numbers([s.get("control") for s in samples if s.get("control")], lambda c: max(abs(c["left_pwm"]), abs(c["right_pwm"])))
    summary = dict(verdict=verdict, counts=counts, max_pwm_seen=max(pwm) if pwm else None, samples=len(samples), duration_s=round(duration, 1),
                   rotated_out_bytes=meta.get("rotated_out_bytes", 0), generated=now_iso(now),
                   latency_kinds={
                       "server_capture_age_ms": stat(cam) if cam else "unknown",
                       "relay_ack_latency_ms": next((c["evidence"] for c in checks if c["id"] == "relay_ack" and c["state"] != "unknown"), "unknown"),
                       "browser_display_age_ms": "unknown (not observable by a passive logger; read the headset HUD)",
                       "physical_timer_ms": next((c["evidence"] for c in checks if c["id"] == "displayed_video_age" and c["state"] != "unknown"), "unknown (needs human mark)"),
                   })
    return checks, summary


# --------------------------------------------------------------------------- rendering

MARK = {"pass": "PASS", "fail": "FAIL", "unknown": "UNKNOWN"}


def render_text(checks, summary):
    lines = [f"FARSIGHT RIG REHEARSAL: {summary['verdict']}  (pass {summary['counts']['pass']}, fail {summary['counts']['fail']}, unknown {summary['counts']['unknown']})",
             f"samples {summary['samples']} over {summary['duration_s'] / 60:.1f} min; generated {summary['generated']}"]
    if summary["rotated_out_bytes"]:
        lines.append(f"note: {summary['rotated_out_bytes']} bytes of older samples were rotated out")
    for kind in ("software", "physical"):
        lines.append(f"\n{kind.upper()} CHECKS" + (" (measured by the logger)" if kind == "software" else " (human-observed; unknown until marked)"))
        for c in (c for c in checks if c["kind"] == kind):
            lines.append(f"  {MARK[c['state']]:<8}{c['title']}\n          {c['evidence']}" + (f"  [{c['at']}]" if c["at"] else ""))
    lines.append("\nLATENCY, kept separate:")
    lines += [f"  {key}: {value}" for key, value in summary["latency_kinds"].items()]
    if summary["verdict"] != "GO":
        lines.append("\nNot fully qualified: only GO means every check passed. Unknown is not pass.")
    return "\n".join(lines)


def render_markdown(checks, summary):
    out = [f"# Rig rehearsal: {summary['verdict']}", "",
           f"pass {summary['counts']['pass']} / fail {summary['counts']['fail']} / unknown {summary['counts']['unknown']}. "
           f"{summary['samples']} samples over {summary['duration_s'] / 60:.1f} min, generated {summary['generated']}.", ""]
    for kind, heading in (("software", "Software checks (measured)"), ("physical", "Physical checks (human-observed)")):
        out += [f"## {heading}", "", "| Result | Check | Evidence | Evidence time |", "|---|---|---|---|"]
        out += [f"| {MARK[c['state']]} | {c['title']} | {c['evidence'].replace('|', '/')} | {c['at'] or '-'} |" for c in checks if c["kind"] == kind]
        out.append("")
    out += ["## Latency kinds (not interchangeable)", ""] + [f"- **{k}**: {v}" for k, v in summary["latency_kinds"].items()]
    return "\n".join(out) + "\n"


def render_html(checks, summary):
    rows = {kind: "".join(
        f"<tr class='{c['state']}'><td>{MARK[c['state']]}</td><td>{html.escape(c['title'])}</td>"
        f"<td>{html.escape(c['evidence'])}</td><td>{c['at'] or '-'}</td></tr>" for c in checks if c["kind"] == kind)
        for kind in ("software", "physical")}
    latency = "".join(f"<li><b>{html.escape(k)}</b>: {html.escape(str(v))}</li>" for k, v in summary["latency_kinds"].items())
    return f"""<!doctype html><html lang="en" data-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rig rehearsal {summary['verdict']}</title><style>
:root{{color-scheme:dark;--bg:#0f1115;--fg:#e6e8ec;--mut:#9aa3b2;--line:#2a2f3a;--pass:#3fb950;--fail:#f85149;--unk:#d29922}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;padding:24px 16px;max-width:1000px}}
h1{{margin:0 0 4px}}.verdict{{font-size:2rem;font-weight:700}}.GO{{color:var(--pass)}}.NO-GO{{color:var(--fail)}}.INCOMPLETE{{color:var(--unk)}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}td{{border-top:1px solid var(--line);padding:6px 8px;vertical-align:top}}
tr.pass td:first-child{{color:var(--pass)}}tr.fail td:first-child{{color:var(--fail)}}tr.unknown td:first-child{{color:var(--unk)}}td:first-child{{font-weight:700}}
p,li{{color:var(--mut)}}@media(max-width:600px){{td:nth-child(4){{display:none}}}}</style></head><body>
<h1>Farsight rig rehearsal</h1><div class="verdict {summary['verdict']}">{summary['verdict']}</div>
<p>pass {summary['counts']['pass']} · fail {summary['counts']['fail']} · unknown {summary['counts']['unknown']} · {summary['samples']} samples over {summary['duration_s'] / 60:.1f} min · generated {summary['generated']}</p>
<p>Only GO means every check passed. Unknown is not pass; physical checks stay unknown until a human marks them.</p>
<h2>Software checks (measured)</h2><table>{rows['software']}</table>
<h2>Physical checks (human-observed)</h2><table>{rows['physical']}</table>
<h2>Latency kinds (not interchangeable)</h2><ul>{latency}</ul></body></html>
"""


def build_report(directory, now=None, **limits):
    directory = Path(directory)
    samples, bad = read_samples(directory)
    checks, summary = evaluate(samples, read_json(directory / "meta.json"), read_json(directory / "marks.json"), now=now,
                               bad_lines=bad, **limits)
    (directory / "report.json").write_text(json.dumps(dict(summary=summary, checks=checks), indent=1))
    (directory / "report.md").write_text(render_markdown(checks, summary))
    (directory / "report.html").write_text(render_html(checks, summary))
    return checks, summary


# --------------------------------------------------------------------------- commands

def record(args, sleep=time.sleep):
    context = tls_context(args.ca_file, args.insecure)
    log = BoundedLog(args.out, args.max_bytes, args.keep)
    deadline = time.monotonic() + args.minutes * 60
    print(f"recording {args.url} every {args.interval}s for {args.minutes} min into {args.out} (passive: GET only)", flush=True)
    try:
        while time.monotonic() < deadline:
            tick = time.monotonic()
            sample = collect_sample(args.url, args.relay, context, args.ssh)
            log.write(sample)
            if sample["errors"]:
                print(f"[{now_iso(sample['t'])}] {', '.join(sample['errors'])}", flush=True)
            sleep(max(0, args.interval - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        print("interrupted; writing report from what was recorded", flush=True)
    finally:
        log.close()
    checks, summary = build_report(args.out, min_minutes=args.min_minutes)
    print(render_text(checks, summary))
    return 0 if summary["verdict"] == "GO" else 1


def mark(args):
    known = dict(PHYSICAL)
    if args.check not in known:
        print(f"unknown check {args.check}; choose from: {', '.join(known)}", file=sys.stderr)
        return 2
    path = Path(args.dir) / "marks.json"
    marks = read_json(path)
    marks[args.check] = dict(state=args.state, by=args.by, note=args.note, at_epoch=round(time.time(), 3))
    path.write_text(json.dumps(marks, indent=1))
    print(f"{args.check}: {args.state} by {args.by}")
    return 0


def report(args):
    checks, summary = build_report(args.dir, min_minutes=args.min_minutes)
    print(render_text(checks, summary))
    print(f"\nwrote {args.dir}/report.md, report.html, report.json")
    return 0 if summary["verdict"] == "GO" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="Passively sample the upstream into a bounded log, then report")
    rec.add_argument("--url", default=os.environ.get("FARSIGHT_URL", "http://127.0.0.1:8443"))
    rec.add_argument("--relay", help="Operator relay origin to probe at /relay/health, e.g. http://127.0.0.1:8080")
    rec.add_argument("--out", required=True)
    rec.add_argument("--minutes", type=float, default=15)
    rec.add_argument("--min-minutes", type=float, default=15, help="Run length the duration check requires")
    rec.add_argument("--interval", type=float, default=1.0)
    rec.add_argument("--max-bytes", type=int, default=2_000_000, help="Rotate samples.jsonl at this size")
    rec.add_argument("--keep", type=int, default=3, help="Rotated files kept")
    rec.add_argument("--ca-file")
    rec.add_argument("--insecure", action="store_true")
    rec.add_argument("--ssh", help="Explicit user@host for a read-only thermal/load read (BatchMode, no sudo)")
    mk = sub.add_parser("mark", help="A human records a physical check result")
    mk.add_argument("--dir", required=True)
    mk.add_argument("check", help="One of: " + ", ".join(cid for cid, _ in PHYSICAL))
    mk.add_argument("state", choices=("pass", "fail"))
    mk.add_argument("--by", required=True)
    mk.add_argument("--note", default="")
    rp = sub.add_parser("report", help="Rebuild the go/no-go report from a recorded directory")
    rp.add_argument("--dir", required=True)
    rp.add_argument("--min-minutes", type=float, default=15)
    args = parser.parse_args(argv)
    sys.exit({"record": record, "mark": mark, "report": report}[args.command](args))


if __name__ == "__main__":
    main()
