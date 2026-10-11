"""PSNR of Brush eval renders (--eval-split-every N --eval-save-to-disk) against the real photos.

  python score_heldout.py IMAGES_DIR EVAL_DIR [EVAL_DIR ...]
"""
import os, sys
import numpy as np, cv2

src = sys.argv[1]
for ev in sys.argv[2:]:
    ps = []
    for n in sorted(os.listdir(ev)):
        gt, r = cv2.imread(f'{src}/{n[:-4]}.jpg'), cv2.imread(f'{ev}/{n}')
        if gt is None or r is None: continue
        if r.shape != gt.shape: r = cv2.resize(r, (gt.shape[1], gt.shape[0]))
        ps.append(10 * np.log10(255 ** 2 / np.mean((r.astype(float) - gt.astype(float)) ** 2)))
    print(f'{ev}: {len(ps)} views, PSNR mean {np.mean(ps):.2f} median {np.median(ps):.2f} min {min(ps):.2f} max {max(ps):.2f}')
