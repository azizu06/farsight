"""Apple Vision vs UltraFace RFB-320 (the rover's exact model + decode) on real rover frames."""
import sys, time, glob, json
import numpy as np, cv2
sys.path.insert(0, '/Users/chin/Documents/code/farsight')
from jetson.face import FaceDetector  # rover's own UltraFace code, conf 0.7, NMS 0.3
import Vision, Quartz
from Foundation import NSURL

frames = sorted(glob.glob(sys.argv[1] + '/*.jpg'))
ultra = FaceDetector('version-RFB-320.onnx')
def vision(path):
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None); img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    req = Vision.VNDetectFaceRectanglesRequest.alloc().init()
    h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    t = time.perf_counter(); h.performRequests_error_([req], None); dt = (time.perf_counter() - t) * 1000
    out = []
    for r in (req.results() or []):
        b = r.boundingBox(); out.append(dict(x=b.origin.x, y=1 - b.origin.y - b.size.height, w=b.size.width, h=b.size.height, conf=float(r.confidence())))
    return out, dt
import tempfile, os
tmp = tempfile.mkdtemp()
res = {}; tu = []; tv = []
for p in frames:
    up = cv2.flip(cv2.imread(p), -1)  # phone is mounted upside down; detect on the upright image
    q = os.path.join(tmp, os.path.basename(p)); cv2.imwrite(q, up, [cv2.IMWRITE_JPEG_QUALITY, 95])
    t = time.perf_counter(); u = ultra.detect(up); tu.append((time.perf_counter() - t) * 1000)
    raw_u = ultra.detect(cv2.imread(p))  # as the rover may see it if it doesn't rotate
    v, dt = vision(q); tv.append(dt)
    res[os.path.basename(p)] = dict(ultra=u, ultra_raw=len(raw_u), vision=v)
s = lambda k: (sum(1 for r in res.values() if r[k]), sum(len(r[k]) for r in res.values()))
fu, nu = s('ultra'); fv, nv = s('vision')
fr = sum(1 for r in res.values() if r['ultra_raw']); nr = sum(r['ultra_raw'] for r in res.values())
print(f'frames {len(res)}')
print(f'UltraFace (upright): frames with a face {fu}, faces {nu}, median {np.median(tu):.1f} ms/frame (CPU, 2 threads)')
print(f'UltraFace (raw upside-down, as sent): frames with a face {fr}, faces {nr}')
print(f'Apple Vision (upright): frames with a face {fv}, faces {nv}, median {np.median(tv):.1f} ms/frame')
json.dump(res, open('face_results.json', 'w'))
