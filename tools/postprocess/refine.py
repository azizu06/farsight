"""Refine images with AI image models over right.codes /draw: refine.py OUT_DIR MODEL IMG [IMG...]"""
import base64, json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

KEY = os.environ["RIGHTCODES_KEY_DRAW"]
DRAW = "https://www.right.codes/draw/v1/images/generations"
TASKS = "https://www.right.codes/v1/tasks/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
PROMPT = ("Restore this exact photo. It is a frame from a small rover's phone camera, or a render of a 3D Gaussian-splat "
          "reconstruction of that scene. Remove blur, noise, JPEG artifacts, splat smears and floating blobs; sharpen edges and "
          "text; fix exposure. Keep the SAME camera angle, framing, orientation, objects, layout and colors. Do not add, remove, "
          "move or restyle anything. Photorealistic.")


def req(url, payload=None, timeout=180):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method="POST" if data else "GET", headers={
        "Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def find(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("b64_json", "b64", "image_base64") and isinstance(v, str):
                return "b64", v
            if (r := find(v)):
                return r
    elif isinstance(o, list):
        for v in o:
            if (r := find(v)):
                return r
    elif isinstance(o, str):
        if o.startswith("data:image"):
            return "b64", o.split(",", 1)[1]
        if o.startswith("http") and o.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".webp")):
            return "url", o
    return None


def refine(model, src, out):
    dst = out / f"{Path(src).stem}.{model}.png"
    if dst.exists():
        return dst.name, "exists"
    img = "data:image/jpeg;base64," + base64.b64encode(Path(src).read_bytes()).decode()
    t0 = time.time()
    try:
        sub = req(DRAW, {"model": model, "prompt": PROMPT, "n": 1, "size": "4:3", "async": True, "image": [img]})
    except Exception as e:
        return dst.name, f"submit failed: {e}"
    tid = sub.get("task_id")
    if not tid:
        kind, val = find(sub) or (None, None)
        if kind is None:
            return dst.name, f"no task_id: {json.dumps(sub)[:300]}"
        t = sub
    else:
        while True:
            if time.time() - t0 > 600:
                return dst.name, "timeout"
            t = req(TASKS + tid)
            if t.get("status") in ("failed", "error"):
                return dst.name, f"failed: {json.dumps(t)[:300]}"
            if t.get("data") or t.get("candidates"):
                break
            time.sleep(4)
    kind, val = find(t) or (None, None)
    if kind == "b64":
        dst.write_bytes(base64.b64decode(val))
    elif kind == "url":
        with urllib.request.urlopen(urllib.request.Request(val, headers={"User-Agent": UA}), timeout=120) as r:
            dst.write_bytes(r.read())
    else:
        return dst.name, f"no image: {json.dumps(t)[:300]}"
    return dst.name, f"ok {time.time() - t0:.0f}s"


if __name__ == "__main__":
    out, models, imgs = Path(sys.argv[1]), sys.argv[2].split(","), sys.argv[3:]
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(m, i) for m in models for i in imgs]
    with ThreadPoolExecutor(8) as ex:
        for name, res in ex.map(lambda j: refine(j[0], j[1], out), jobs):
            print(name, res, flush=True)
