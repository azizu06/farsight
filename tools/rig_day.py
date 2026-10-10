"""One-command rig-day sequence: runs the existing checks in order into one folder.

python -m tools.rig_day --url http://JETSON:8443 [--relay http://127.0.0.1:8080] --out runs-rig/day1
python -m tools.rig_day --url ... --out /tmp/x --dry-run          # print the exact commands only
python -m tools.rig_day --url ... --out /tmp/x --skip preflight --skip ready

Steps, in order, each skippable with --skip NAME:
  preflight, ready, stream, detector, qualify_link, pitch_metrics, rehearse

Each step is the existing tool, called as-is, with its output saved under --out.
Nothing here sends a drive command or holds a permit: qualify_link and
pitch_metrics send stop-only probes (pitch_metrics without --hold-permit), and
both are refused while another control socket owner is connected (or when that
cannot be checked). rehearse starts `tools.rehearse_demo record`, which is
passive; mark the physical checks afterwards with `tools.rehearse_demo mark`.
summary.md lists PASS/FAIL/SKIPPED per step. Exit 0 only when no step failed.
"""

import argparse
import json
import shlex
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

from .netutil import fetch_json, tls_context

STEPS = ("preflight", "ready", "stream", "detector", "qualify_link", "pitch_metrics", "rehearse")
CONTROL_PROBES = ("qualify_link", "pitch_metrics")


def tool(module, *args):
    return [sys.executable, "-m", f"tools.{module}", *map(str, args)]


def tls_args(args, insecure=True):
    out = ["--ca-file", args.ca_file] if args.ca_file else []
    return out + (["--insecure"] if args.insecure and insecure else [])


def build_plan(args):
    """[(step, argv)] in run order for every step, skipped or not."""
    out = Path(args.out)
    probe_url = args.relay or args.url
    seconds = str(args.seconds)
    return [
        ("preflight", tool("preflight", "--role", args.role)),
        ("ready", tool("ready", "--url", args.url, "--timeout", args.ready_timeout, *tls_args(args),
                       *([] if args.allow_cpu_detector else ["--require-tensorrt"]),
                       *(["--skip", args.ready_skip] if args.ready_skip else []))),
        ("stream", tool("measure", "stream", "--url", args.url, "--seconds", seconds, *tls_args(args))),
        ("detector", tool("measure", "detector", "--url", args.url, "--seconds", seconds, *tls_args(args))),
        ("qualify_link", tool("qualify_link", "--url", probe_url, "--seconds", seconds,
                              *tls_args(args, insecure=False), "--output", out / "qualify_link.json")),
        ("pitch_metrics", tool("pitch_metrics", "--url", probe_url, "--seconds", seconds, *tls_args(args, insecure=False),
                               "--output", out / "pitch.json", "--markdown", out / "pitch.md")),
        ("rehearse", tool("rehearse_demo", "record", "--url", args.url, *(["--relay", args.relay] if args.relay else []),
                          "--out", out / "rehearsal", "--minutes", args.rehearse_minutes,
                          "--min-minutes", args.rehearse_minutes, *tls_args(args),
                          *(["--ssh", args.ssh] if args.ssh else []))),
    ]


def control_owner(url, context=None, fetch=fetch_json):
    """None when no control socket is owned; otherwise a reason string. Unreachable counts as unverifiable."""
    try:
        status = fetch(url, "/control/status", context)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return f"cannot verify control owner ({type(exc).__name__})"
    if not isinstance(status, dict):
        return "cannot verify control owner (bad /control/status)"
    if status.get("session_id"):
        return "another control socket owner is connected (close the Quest/operator page first)"
    return None


def run_step(step, argv, out, args, runner=subprocess.run, owner_check=control_owner):
    """Return (state, detail). state is PASS, FAIL or SKIPPED."""
    if step in args.skip:
        return "SKIPPED", "--skip"
    if step in CONTROL_PROBES:
        blocked = owner_check(args.probe_status_url, tls_context(args.ca_file, args.insecure))
        if blocked:
            return "FAIL", f"refused, not run: {blocked}"
    log = out / f"{STEPS.index(step) + 1:02d}-{step}.log"
    with open(log, "w") as handle:
        handle.write("$ " + shlex.join(map(str, argv)) + "\n")
        handle.flush()
        try:
            done = runner(argv, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except OSError as exc:
            return "FAIL", f"could not start: {exc}"
    code = done.returncode
    detail = f"exit {code}, log {log.name}"
    if step == "rehearse":
        verdict = rehearsal_verdict(out / "rehearsal")
        detail += f"; verdict {verdict}"
    return ("PASS" if code == 0 else "FAIL"), detail


def rehearsal_verdict(directory):
    try:
        return json.loads((Path(directory) / "report.json").read_text())["summary"]["verdict"]
    except (OSError, ValueError, KeyError, TypeError):
        return "no report"


def render_summary(results, args, started):
    lines = ["# Rig day summary", "",
             f"url {args.url}" + (f", relay {args.relay}" if args.relay else "") +
             f", started {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started))}", "",
             "| Step | Result | Detail |", "|---|---|---|"]
    lines += [f"| {step} | {state} | {detail.replace('|', '/')} |" for step, state, detail in results]
    lines += ["", "PASS means the tool exited 0, not that the physical rig is qualified. The rehearsal verdict is GO only "
              "when every software and physical check passes; mark the physical ones with "
              f"`python -m tools.rehearse_demo mark --dir {Path(args.out) / 'rehearsal'} CHECK pass --by NAME`."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Jetson server origin (board profile: http://192.168.55.1:8443)")
    parser.add_argument("--relay", help="Operator relay origin; qualify_link and pitch_metrics probe it instead of --url")
    parser.add_argument("--out", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Print the command list and run nothing")
    parser.add_argument("--skip", action="append", default=[], choices=STEPS, metavar="STEP",
                        help="Skip a step (repeatable): " + ", ".join(STEPS))
    parser.add_argument("--role", default="auto", choices=("auto", "operator", "rover", "both"))
    parser.add_argument("--seconds", type=float, default=10, help="Seconds for stream, detector, qualify_link, pitch_metrics")
    parser.add_argument("--ready-timeout", type=float, default=120)
    parser.add_argument("--ready-skip", help="Rows tools.ready ignores, e.g. rover,video")
    parser.add_argument("--allow-cpu-detector", action="store_true",
                        help="Do not require the TensorRT detector in the ready step (Mac or CPU rehearsal only)")
    parser.add_argument("--rehearse-minutes", type=float, default=15)
    parser.add_argument("--ca-file")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--ssh", help="Explicit user@host for the rehearsal's read-only thermal read")
    args = parser.parse_args(argv)
    args.probe_status_url = args.url
    plan = build_plan(args)
    if args.dry_run:
        for step, command in plan:
            print(("SKIPPED " if step in args.skip else "") + f"{step}: " + shlex.join(map(str, command)))
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    results = []
    for step, command in plan:
        print(f"[{step}] ...", flush=True)
        state, detail = run_step(step, command, out, args)
        print(f"[{step}] {state}: {detail}", flush=True)
        results.append((step, state, detail))
        (out / "summary.md").write_text(render_summary(results, args, started))
    print(f"wrote {out / 'summary.md'}")
    return 1 if any(state == "FAIL" for _, state, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
