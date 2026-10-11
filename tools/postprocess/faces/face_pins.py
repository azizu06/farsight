"""Face detection for the walkthrough: one command from a capture to faces.json + boxed photos for walk.html.

  python face_pins.py RUN_DIR DOTS_PLY OUT_DIR
RUN_DIR: capture with images/ + transforms.json. DOTS_PLY: the run's LiDAR dots (init.ply), same ARKit session.
Writes OUT_DIR/faces.json and OUT_DIR/faceframes/face_N.jpg; open walk.html?...&faces=faces.json from OUT_DIR.

Detector: YOLO11n-pose keypoint heads (nose/eyes/ears, conf > 0.5, >= 3 points; square box, side = 1.4 x span),
the method chosen on 2026-10-11 (#235). Frames are flipped upright first (the phone is mounted upside down).
Each head is placed in 3D by casting a ray from that frame's camera onto the LiDAR dots; sightings within 0.6 m
merge into one face spot (seen in >= 2 frames). Note: people who move show up as separate spots.
"""
import base64, json, os, sys
import numpy as np, cv2, open3d as o3d
from ultralytics import YOLO

run, dots, out = sys.argv[1:4]
os.makedirs(f'{out}/faceframes', exist_ok=True)
frames = json.load(open(f'{run}/transforms.json'))['frames']
pose = YOLO('yolo11n-pose.pt')
pc = o3d.io.read_point_cloud(dots); tree = o3d.geometry.KDTreeFlann(pc); P = np.asarray(pc.points)

def heads(img):
    r = pose.predict(img, conf=0.4, verbose=False)[0]; H, W = img.shape[:2]; out = []
    if r.keypoints is None or not len(r.keypoints): return out
    for kp, kc in zip(r.keypoints.xy.tolist(), r.keypoints.conf.tolist()):
        pts = [(x, y) for (x, y), c in zip(kp[:5], kc[:5]) if c > 0.5]
        if len(pts) < 3: continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]; s = max(max(xs) - min(xs), 8) * 1.4
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        out.append(dict(x=(cx - s / 2) / W, y=(cy - s / 2) / H, w=s / W, h=s / H))
    return out

hits = []
for f in frames:
    name = os.path.basename(f['file_path']); img = cv2.flip(cv2.imread(f"{run}/{f['file_path']}"), -1)
    M = np.array(f['transform_matrix']); W, H = f['w'], f['h']; o = M[:3, 3]
    for b in heads(img):
        u, v = W - (b['x'] + b['w'] / 2) * W, H - (b['y'] + b['h'] / 2) * H   # upright -> raw camera pixels
        d = M[:3, :3] @ np.array([(u - f['cx']) / f['fl_x'], -(v - f['cy']) / f['fl_y'], -1.0]); d /= np.linalg.norm(d)
        hit = None
        for t in np.arange(0.3, 10.0, 0.03):
            k, idx, _ = tree.search_radius_vector_3d(o + d * t, 0.06)
            if k: hit = P[np.asarray(idx)].mean(0); break
        if hit is not None:
            hits.append(dict(frame=name, path=f['file_path'], p=hit, cam=o, yaw=float(np.arctan2(M[0, 2], M[2, 2])), box=b, area=b['w'] * b['h']))
spots = []
for h in sorted(hits, key=lambda h: -h['area']):
    for g in spots:
        if np.linalg.norm(g['p'] - h['p']) < 0.6: g['n'] += 1; g['ps'].append(h['p']); break
    else: spots.append(dict(h, n=1, ps=[h['p']]))
faces = []
for i, g in enumerate(sorted([g for g in spots if g['n'] >= 2], key=lambda g: -g['n'])):
    im = cv2.flip(cv2.imread(f"{run}/{g['path']}"), -1); H, W = im.shape[:2]; b = g['box']
    x, y, w, h = int(b['x'] * W), int(b['y'] * H), int(b['w'] * W), int(b['h'] * H)
    crop = cv2.resize(im[max(0, y - h):min(H, y + 2 * h), max(0, x - w):min(W, x + 2 * w)], (96, 96))
    thumb = 'data:image/jpeg;base64,' + base64.b64encode(cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 80])[1]).decode()
    cv2.rectangle(im, (x - 2, y - 2), (x + w + 2, y + h + 2), (63, 210, 255), 3)
    cv2.rectangle(im, (x - 2, max(0, y - 26)), (x + 90, y - 2), (63, 210, 255), -1)
    cv2.putText(im, f'Face {i + 1}', (x + 4, max(16, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2, cv2.LINE_AA)
    cv2.imwrite(f'{out}/faceframes/face_{i + 1}.jpg', im, [cv2.IMWRITE_JPEG_QUALITY, 92])
    faces.append(dict(id=i + 1, pos=[round(float(v), 3) for v in np.median(np.array(g['ps']), 0)], seen=g['n'], frame=g['frame'],
                      frame_img=f'faceframes/face_{i + 1}.jpg', view=[round(float(v), 3) for v in g['cam']] + [round(g['yaw'], 3)], thumb=thumb))
json.dump(dict(method='YOLO11n-pose keypoint heads', sightings=len(hits), faces=faces), open(f'{out}/faces.json', 'w'))
print(f'{len(hits)} sightings -> {len(faces)} face spots -> {out}/faces.json')
