/** COCO-17 limb pairs: face, arms, torso, legs. */
export const POSE_EDGES = [
  [0, 1], [0, 2], [1, 3], [2, 4], [5, 6], [5, 7], [7, 9], [6, 8], [8, 10],
  [5, 11], [6, 12], [11, 12], [11, 13], [13, 15], [12, 14], [14, 16],
];
export const KEYPOINT_MIN_CONF = 0.3;
export const LINK_LOST_MS = 500;

/** Valid detector keypoints or null; absent/malformed pose is simply not drawn. */
export function parseKpts(kpts) {
  if (!Array.isArray(kpts) || kpts.length !== 17) return null;
  for (const p of kpts) {
    if (!Array.isArray(p) || p.length < 3 || !p.slice(0, 3).every(Number.isFinite)) return null;
  }
  return kpts;
}

/**
 * Skeleton limbs as flat [x1,y1,x2,y2,...] in cropped-view 0..1 coordinates.
 * A limb needs both ends confident and inside the measured crop; nothing is invented.
 */
export function poseSegments(kpts, crop, sourceWidth, sourceHeight, minConf = KEYPOINT_MIN_CONF) {
  const points = parseKpts(kpts);
  const out = [];
  if (!points) return out;
  const view = points.map(([x, y, conf]) => {
    if (conf < minConf) return null;
    const vx = (x * sourceWidth - crop.x) / crop.width, vy = (y * sourceHeight - crop.y) / crop.height;
    return vx >= 0 && vx <= 1 && vy >= 0 && vy <= 1 ? [vx, vy] : null;
  });
  for (const [a, b] of POSE_EDGES) {
    if (view[a] && view[b]) out.push(view[a][0], view[a][1], view[b][0], view[b][1]);
  }
  return out;
}

/** One path, one stroke per frame for every person. */
export function strokeSegments(ctx, segments, width, height, color, lineWidth) {
  if (!segments.length) return;
  ctx.beginPath();
  for (let i = 0; i < segments.length; i += 4) {
    ctx.moveTo(segments[i] * width, segments[i + 1] * height);
    ctx.lineTo(segments[i + 2] * width, segments[i + 3] * height);
  }
  ctx.strokeStyle = color; ctx.lineWidth = lineWidth; ctx.lineCap = "round";
  ctx.stroke();
}

/** Link chip: red when the Jetson has not replied for 500 ms (or never connected). */
export function linkHealth({ now, receivedAt, connected, linkMs, detFps }) {
  const age = connected && Number.isFinite(receivedAt) ? now - receivedAt : Infinity;
  const ok = age < LINK_LOST_MS;
  const parts = [ok ? "LINK OK" : "LINK LOST"];
  if (ok && Number.isFinite(linkMs)) parts.push(`${Math.round(linkMs)} ms`);
  if (Number.isFinite(detFps)) parts.push(`det ${detFps.toFixed(1)} fps`);
  return { ok, text: parts.join(" · "), color: ok ? "#a6edc4" : "#ff4d4d" };
}
