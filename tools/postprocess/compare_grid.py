"""Labeled side-by-side: real photo | trained splat | other method, with per-image PSNR, from Brush eval dirs.

  python compare_grid.py IMAGES_DIR OURS_EVAL OTHER_EVAL OUT.jpg [name ...]
Images are flipped 180 deg for display (the phone is mounted upside down).
"""
import sys, os
import numpy as np, cv2
src, ours, other, out = sys.argv[1:5]
names = sys.argv[5:] or sorted(os.listdir(ours))[4::22][:4]
def psnr(a, b): return 10 * np.log10(255 ** 2 / np.mean((a.astype(float) - b.astype(float)) ** 2))
W, H, HEAD, ROW = 400, 300, 56, 26
canvas = np.full((HEAD + len(names) * (H + ROW), W * 3, 3), 24, np.uint8)
for c, t in enumerate(['Real photo', 'Trained photo splat', 'Other method']):
    cv2.putText(canvas, t, (c * W + 10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
for r, n in enumerate(names):
    n = n.replace('.png', ''); gt = cv2.imread(f'{src}/{n}.jpg'); y = HEAD + r * (H + ROW)
    cv2.putText(canvas, f'Photo #{int(n)} (held out)', (10, y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)
    for c, img in enumerate([gt, cv2.imread(f'{ours}/{n}.png'), cv2.imread(f'{other}/{n}.png')]):
        if img.shape[:2] != gt.shape[:2]: img = cv2.resize(img, (gt.shape[1], gt.shape[0]))
        tile = cv2.resize(cv2.flip(img, -1), (W, H))
        if c: cv2.putText(tile, f'{psnr(img, gt):.1f} dB', (8, H - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
        canvas[y + ROW:y + ROW + H, c * W:(c + 1) * W] = tile
cv2.imwrite(out, canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])
