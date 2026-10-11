"""YOLO11n-face, SCRFD-500M, SCRFD-10G and YOLO11n-pose head boxes on the same upright rover frames (Mac CPU)."""
import sys, glob, os, time, json
import numpy as np, cv2
from ultralytics import YOLO
from insightface.model_zoo import get_model
frames = sorted(glob.glob(sys.argv[1] + '/*.jpg'))
yf = YOLO('yface.pt'); yp = YOLO('yolo11n-pose.pt')
s500 = get_model(os.path.expanduser('~/.insightface/models/buffalo_sc/det_500m.onnx'), providers=['CPUExecutionProvider']); s500.prepare(ctx_id=-1, input_size=(640, 640), det_thresh=0.5)
s10g = get_model(os.path.expanduser('~/.insightface/models/buffalo_l/det_10g.onnx'), providers=['CPUExecutionProvider']); s10g.prepare(ctx_id=-1, input_size=(640, 640), det_thresh=0.5)
res = json.load(open('face_results.json')); T = {k: [] for k in ['yolo_face', 'scrfd_500m', 'scrfd_10g', 'pose_head']}
def norm(b, W, H, c): x1, y1, x2, y2 = b; return dict(x=float(x1 / W), y=float(y1 / H), w=float((x2 - x1) / W), h=float((y2 - y1) / H), conf=float(c))
for p in frames:
    k = os.path.basename(p); im = cv2.flip(cv2.imread(p), -1); H, W = im.shape[:2]; r = res[k]
    t = time.perf_counter(); o = yf.predict(im, conf=0.4, verbose=False)[0]; T['yolo_face'].append((time.perf_counter() - t) * 1000)
    r['yolo_face'] = [norm(b, W, H, c) for b, c in zip(o.boxes.xyxy.tolist(), o.boxes.conf.tolist())]
    for name, m in (('scrfd_500m', s500), ('scrfd_10g', s10g)):
        t = time.perf_counter(); b, _ = m.detect(im, max_num=0, metric='default'); T[name].append((time.perf_counter() - t) * 1000)
        r[name] = [norm(x[:4], W, H, x[4]) for x in b]
    t = time.perf_counter(); o = yp.predict(im, conf=0.4, verbose=False)[0]; T['pose_head'].append((time.perf_counter() - t) * 1000)
    heads = []
    if o.keypoints is not None and len(o.keypoints):
        for kp, kc in zip(o.keypoints.xy.tolist(), o.keypoints.conf.tolist()):
            pts = [(x, y) for (x, y), c in zip(kp[:5], kc[:5]) if c > 0.5]  # nose, eyes, ears
            if len(pts) >= 3:
                xs, ys = [q[0] for q in pts], [q[1] for q in pts]; s = max(max(xs) - min(xs), 8) * 1.4
                cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
                heads.append(norm((cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2), W, H, np.mean([c for c in kc[:5] if c > 0.5])))
    r['pose_head'] = heads
json.dump(res, open('face_results_all.json', 'w'))
print({k: round(float(np.median(v)), 1) for k, v in T.items()}, 'ms/frame median, Mac M2 CPU')
