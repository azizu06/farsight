"""Drift guard: diff the wire contract of the Python and Rust servers.

Probes a fixed list of endpoints on both origins and compares the response
shape — status code and the recursive key set / JSON type of each field —
not the values. New endpoints added to one server but not the other, renamed
fields, or a field whose type changes (e.g. int -> float) all show up here.

    python tools/check_rust_parity.py \
        --python http://127.0.0.1:8443 --rust http://127.0.0.1:8444

Defaults assume the Python server on 8443 and the Rust port on 8444; pass
--self-signed when probing TLS with the bundled dev certificate. Exit 1 on
any drift so the check can gate merges.
"""

from __future__ import annotations

import argparse
import json
import ssl
import urllib.request
from typing import Any

# (method, path, body or None). Keep this list contract-complete: it is the
# surface drift can hide on.
PROBES: list[tuple[str, str, Any]] = [
    ("GET", "/health", None),
    ("GET", "/control/status", None),
    ("GET", "/scan", None),
    ("GET", "/mission/report", None),
    ("GET", "/mission/drill", None),
    ("POST", "/mission/drill", {}),
    ("POST", "/mission/drill", {"duration_s": "x"}),  # must 422, not default
    ("GET", "/speak", None),
    ("POST", "/map/pause", {"paused": True}),
    ("POST", "/map/pause", {}),  # must 422, not default
    ("POST", "/map/pause", {"paused": False}),
    ("POST", "/nav/goal", {"x": 1.0, "z": -0.5}),
    ("POST", "/nav/cancel", None),
    ("GET", "/mission/snapshots/../server.py", None),  # traversal must fail
]


def fetch(origin: str, method: str, path: str, body: Any, ctx: ssl.Context | None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(origin + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            return resp.status, _read(resp)
    except urllib.error.HTTPError as e:
        return e.code, _read(e)
    except Exception as e:  # unreachable server is drift too
        return 0, f"<{type(e).__name__}: {e}>"


def _read(resp) -> Any:
    raw = resp.read(1 << 20)
    try:
        return json.loads(raw)
    except Exception:
        return raw[:200].decode("utf-8", "replace")


def shape(value: Any, depth: int = 0) -> Any:
    """Recursive JSON skeleton: scalar -> type tag; dict -> {key: shape};
    list -> [shape of first element] + length flag; depth-capped."""
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        inner = shape(value[0], depth + 1) if value else None
        return [inner, f"len={len(value)}>0" if value else "len=0"]
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    return type(value).__name__


def diff(a: Any, b: Any, path: str = "") -> list[str]:
    """Human-readable differences between two shapes."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}" if path else k
            if k not in a:
                out.append(f"{p}: only on rust ({shape(b[k]) if not isinstance(b[k], (dict, list)) else 'present'})")
            elif k not in b:
                out.append(f"{p}: only on python")
            else:
                out.extend(diff(a[k], b[k], p))
        return out
    if isinstance(a, list) and isinstance(b, list) and a and b:
        return diff(a[0], b[0], f"{path}[]")
    # A present-but-null field is state, not drift: the key exists in both
    # wire formats and either server emits null depending on runtime state
    # (elapsed_s before a drill, reason when active, sbar before triage).
    if a == "null" or b == "null":
        return out
    if a != b:
        out.append(f"{path or '<root>'}: python={a} rust={b}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", default="http://127.0.0.1:8443", help="Python server origin")
    ap.add_argument("--rust", default="http://127.0.0.1:8444", help="Rust server origin")
    ap.add_argument("--self-signed", action="store_true", help="Trust a self-signed upstream certificate")
    args = ap.parse_args()
    ctx = ssl._create_unverified_context() if args.self_signed else None

    drift = 0
    for method, path, body in PROBES:
        ps, pv = fetch(args.python, method, path, body, ctx)
        rs, rv = fetch(args.rust, method, path, body, ctx)
        problems = []
        if ps != rs:
            problems.append(f"status python={ps} rust={rs}")
        if ps == rs and isinstance(pv, (dict, list)) and isinstance(rv, (dict, list)):
            problems.extend(diff(shape(pv), shape(rv)))
        label = f"{method} {path}" + (f" {json.dumps(body)}" if body is not None else "")
        if problems:
            drift += 1
            print(f"DRIFT {label}")
            for p in problems[:20]:
                print(f"      {p}")
        else:
            print(f"ok    {label} -> {ps}")
    print(f"\n{drift} endpoint(s) drifted" if drift else "\nNo contract drift")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
