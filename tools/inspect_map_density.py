"""GET-only density evidence across the operator, geometry and Mac renderer hops.

Run ``python tools/inspect_map_density.py`` for the three local default hops, or
select endpoints: ``--operator http://MAC:8080 --renderer http://MAC:8090``.
An explicitly supplied --phone URL probes the phone's /geometry/status contract.
Only an operator /map/status HTTP 404 triggers a same-origin /health fallback;
both attempts and the actual evidence URL are recorded under the shared deadline.
No binary geometry, producer, control, scan, pause or reset requests are made.
Reported mesh vertices are not deduplicated measured LiDAR points. Unknown fields
stay null; this snapshot alone is not hardware or visual qualification.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import aiohttp


ENDPOINTS = {
    "operator": ("http://127.0.0.1:8080", "/map/status"),
    "geometry": ("http://127.0.0.1:8484", "/geometry/status"),
    "renderer": ("http://127.0.0.1:8090", "/health"),
    "phone": (None, "/geometry/status"),
}
DEFAULT_MAX_BYTES = 256 * 1024


def finite_json_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"nonfinite JSON number: {value}")
    return parsed


def endpoint_url(name, value):
    """Only accept the documented GET route for this named hop."""
    parts = urlsplit(value)
    expected = ENDPOINTS[name][1]
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.path not in ("", "/", expected)):
        raise ValueError(f"{name}: supply an HTTP(S) origin or its {expected} endpoint, without credentials/query/fragment")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError(f"{name}: invalid port") from exc
    return urlunsplit((parts.scheme, parts.netloc, expected, "", ""))


def field(body, *paths, kind=None):
    for path in paths:
        value = body
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            continue
        valid = (kind != "count" or type(value) is int and value >= 0)
        valid &= (kind != "bool" or type(value) is bool)
        valid &= (kind != "str" or isinstance(value, str) and bool(value))
        valid &= (kind != "number" or type(value) in (int, float) and math.isfinite(value) and value >= 0)
        if valid:
            return {"value": value, "path": path}
    return {"value": None, "path": None}


def evidence(name, body):
    """Map only documented counts or unambiguous explicitly named fields."""
    result = {
        "source": field(body, "geometry_source", "source", "geometry.source", kind="str"),
        "capture_source": field(body, "source", kind="str"),
        "session": field(body, "session_id", "session", kind="str"),
        "epoch": field(body, "map_epoch", "epoch", kind="count"),
        "revision": field(body, "revision", kind="count"),
        "connected": field(body, "geometry.connected", "connected", kind="bool"),
        "snapshot_complete": field(body, "geometry.snapshot_complete", "snapshot_complete", kind="bool"),
        "retained_unique_points": field(body, "retained_unique_points", "retained.unique_points", kind="count"),
        "point_observations": field(body, "point_observations", kind="count"),
        "retained_points": field(body, "retained.points", "retained_points", kind="count"),
        "retained_vertices": field(body, "retained.vertices", "vertices", kind="count"),
        "retained_triangles": field(body, "retained.triangles", "triangles", kind="count"),
        "retained_anchors": field(body, "retained.anchors", "geometry.anchors", "anchors", kind="count"),
        "retained_bytes": field(body, "retained.bytes", "retained_bytes", kind="count"),
        "displayed_points": field(body, "displayed_points", kind="count"),
        "points_cap": field(body, "points_cap", "point_cap", kind="count"),
        "triangles_cap": field(body, "triangles_cap", kind="count"),
        "capped": field(body, "capped", kind="bool"),
        "dropped": field(body, "dropped", kind="count"),
        "voxel_m": field(body, "voxel_m", kind="number"),
        "stereo": field(body, "stereo", kind="bool"),
        "ready": field(body, "ready", kind="bool"),
    }
    # /map/status points are retained voxel cells for RGB-D, but mesh vertices
    # for geometry_relay. Neither field proves the number drawn by the renderer.
    if name == "operator":
        points = field(body, "points", kind="count")
        if result["source"]["value"] == "IPHONE ARKIT MESH":
            result["retained_vertices"] = points
        elif result["voxel_m"]["value"] is not None:
            result["retained_points"] = points
            if result["retained_unique_points"]["value"] is None:
                result["retained_unique_points"] = points
    result["points_semantics"] = (
        "retained voxel cells (spatially deduplicated; source label determines measured/estimated)"
        if name == "operator" and result["voxel_m"]["value"] is not None
        and result["source"]["value"] != "IPHONE ARKIT MESH"
        else "retained mesh vertices; spatial uniqueness and measured depth samples are unproven"
        if name in ("geometry", "phone") or result["source"]["value"] == "IPHONE ARKIT MESH"
        else "only explicitly named reported counts are normalized")
    return result


async def probe(client, name, url, max_bytes):
    started = time.monotonic()
    result = dict(name=name, url=url, method="GET", requested_at_utc=datetime.now(timezone.utc).isoformat(),
                  ok=False, http_status=None, response_bytes=0, response=None, evidence=None, error=None)
    try:
        async with client.get(url, allow_redirects=False, headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
            result["http_status"] = response.status
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("encoded response rejected; requested identity")
            if response.content_length is not None and response.content_length > max_bytes:
                raise ValueError(f"response exceeds {max_bytes} byte limit (Content-Length={response.content_length})")
            chunks = []
            async for chunk in response.content.iter_chunked(min(max_bytes + 1, 16384)):
                result["response_bytes"] += len(chunk)
                if result["response_bytes"] > max_bytes:
                    raise ValueError(f"response exceeds {max_bytes} byte limit")
                chunks.append(chunk)
            data = b"".join(chunks)
            if response.status != 200:
                result["response_excerpt"] = data[:1024].decode("utf-8", errors="replace")
                raise ValueError(f"HTTP {response.status}; redirects are not followed")
            body = json.loads(data, parse_float=finite_json_float,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {value}")))
            if not isinstance(body, dict):
                raise ValueError("status JSON must be an object")
            result.update(ok=True, response=body, evidence=evidence(name, body))
    except asyncio.CancelledError:
        result["error"] = "overall timeout exhausted"
    except asyncio.TimeoutError:
        result["error"] = "overall timeout exhausted while reading endpoint"
    except (aiohttp.ClientError, ValueError, UnicodeError, RecursionError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def attempt_summary(result):
    return {key: result[key] for key in ("url", "method", "requested_at_utc", "ok", "http_status",
                                         "response_bytes", "error", "elapsed_ms", "response_excerpt")
            if key in result}


async def probe_hop(client, name, url, max_bytes, deadline):
    started = time.monotonic()
    result = await probe(client, name, url, max_bytes)
    attempts = [attempt_summary(result)]
    fallback = name == "operator" and result["http_status"] == 404
    if fallback and time.monotonic() < deadline:
        parts = urlsplit(url)
        health_url = urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))
        result = await probe(client, name, health_url, max_bytes)
        attempts.append(attempt_summary(result))
    result.update(requested_url=url, attempts=attempts, fallback_attempted=len(attempts) > 1,
                  elapsed_ms=round((time.monotonic() - started) * 1000, 2))
    if fallback:
        result["fallback_reason"] = "Operator /map/status returned HTTP 404; legacy same-origin GET /health compatibility fallback"
    return result


def identity(session):
    try:
        return str(uuid.UUID(session))
    except (ValueError, AttributeError):
        return session


def findings(hops):
    result = []
    good = [hop for hop in hops if hop["ok"]]
    for hop in hops:
        if hop.get("fallback_attempted"):
            result.append(dict(kind="legacy_operator_health_fallback", hops=[hop["name"]],
                               detail="/map/status returned HTTP 404; evidence/error comes from the recorded same-origin /health attempt"))
        if not hop["ok"]:
            result.append(dict(kind="unavailable", hops=[hop["name"]], detail=hop["error"]))
            continue
        reported = hop["evidence"]
        if reported["capped"]["value"] is True:
            result.append(dict(kind="cap_reported", hops=[hop["name"]], detail="Endpoint reports capped=true; affected count/limit may be unknown"))
        if reported["snapshot_complete"]["value"] is False:
            result.append(dict(kind="partial_snapshot", hops=[hop["name"]], detail="Endpoint reports incomplete geometry snapshot"))
        for count_key, cap_key in (("retained_points", "points_cap"), ("retained_triangles", "triangles_cap")):
            count, cap = reported[count_key]["value"], reported[cap_key]["value"]
            if count is not None and cap is not None and cap > 0 and count > cap:
                result.append(dict(kind="reported_count_exceeds_cap", hops=[hop["name"]], count=count_key,
                                   value=count, cap=cap, detail="Retained count exceeds reported export/display cap; actual displayed count is separate"))
    for index, left in enumerate(good):
        for right in good[index + 1:]:
            for key in ("session", "epoch", "source"):
                a, b = left["evidence"][key]["value"], right["evidence"][key]["value"]
                if a is None or b is None:
                    continue
                comparable = (identity(a), identity(b)) if key == "session" else (a, b)
                if comparable[0] != comparable[1]:
                    result.append(dict(kind=f"{key}_disagreement", hops=[left["name"], right["name"]],
                                       values=[a, b], detail="Observed independently; source labels may describe different lanes and sampling is not atomic"))
    return result


async def inspect(endpoints, timeout=5.0, max_bytes=DEFAULT_MAX_BYTES):
    if not math.isfinite(timeout) or timeout <= 0 or max_bytes < 1:
        raise ValueError("timeout must be finite and positive; max_bytes must be positive")
    urls = [(name, endpoint_url(name, value)) for name, value in endpoints.items()]
    started = time.monotonic()
    deadline = started + timeout
    async with aiohttp.ClientSession(auto_decompress=False, trust_env=False,
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as client:
        tasks = [asyncio.create_task(probe_hop(client, name, url, max_bytes, deadline)) for name, url in urls]
        try:
            hops = await asyncio.wait_for(asyncio.gather(*tasks), timeout=max(0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            # probe records cancellation, and wait_for waits for cancellation cleanup.
            hops = [task.result() for task in tasks]
    return dict(version=1, scope="GET-only per-hop density snapshot; not visual/performance or hardware qualification",
                sampled_at_utc=datetime.now(timezone.utc).isoformat(), timeout_seconds=timeout,
                max_response_bytes=max_bytes, elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                all_endpoints_responded=all(hop["ok"] for hop in hops), hops=hops, findings=findings(hops),
                limitations=["Null values are unknown, never configuration defaults or assumed zeros",
                             "Mesh vertex counts are not unique measured LiDAR points; observation counts are separate",
                             "Independent GET snapshots are not atomic; no binary payload or headset draw count was inspected"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, (default, _) in ENDPOINTS.items():
        parser.add_argument(f"--{name}", nargs="?" if default else None, const=default,
                            help=f"Probe only selected hops; origin or full endpoint{'; default ' + default if default else ''}")
    parser.add_argument("--timeout", type=float, default=5.0, help="Overall wall-clock timeout in seconds (default 5)")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="Maximum response bytes per hop")
    args = parser.parse_args(argv)
    selected = {name: getattr(args, name) for name in ENDPOINTS if getattr(args, name) is not None}
    if not selected:
        selected = {name: default for name, (default, _) in ENDPOINTS.items() if default}
    try:
        result = asyncio.run(inspect(selected, args.timeout, args.max_bytes))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["all_endpoints_responded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
