/** A fixed camera can only look within its measured frame: never invent pixels. */
export function cameraCrop(width, height, pan, digital = false) {
  if (![width, height, pan].every(Number.isFinite) || width <= 0 || height <= 0)
    throw new Error("Invalid camera crop geometry");
  if (!digital) return { x: 0, y: 0, width, height };
  const croppedWidth = width * 0.7;
  const croppedHeight = height * 0.7;
  const yaw = Math.max(-80, Math.min(80, pan)) / 80;
  return {
    x: (width - croppedWidth) * (yaw + 1) / 2,
    y: (height - croppedHeight) / 2,
    width: croppedWidth, height: croppedHeight,
  };
}

/** Clip observed normalized boxes to the chosen measured crop. */
export function croppedBox(box, crop, sourceWidth, sourceHeight) {
  if (![box.x, box.y, box.w, box.h].every(Number.isFinite) || box.w <= 0 || box.h <= 0) return null;
  const left = Math.max(box.x * sourceWidth, crop.x);
  const top = Math.max(box.y * sourceHeight, crop.y);
  const right = Math.min((box.x + box.w) * sourceWidth, crop.x + crop.width);
  const bottom = Math.min((box.y + box.h) * sourceHeight, crop.y + crop.height);
  if (right <= left || bottom <= top) return null;
  return { x: (left - crop.x) / crop.width, y: (top - crop.y) / crop.height,
    w: (right - left) / crop.width, h: (bottom - top) / crop.height };
}

/** Match actual decoder PTS to a capture identity, allowing only clock quantization. */
export function sampleFrame(samples, mediaTime, segmentStart, segmentEnd) {
  if (!Array.isArray(samples) || !samples.length || samples.length > 60 ||
      ![mediaTime, segmentStart, segmentEnd].every(Number.isFinite)) return null;
  const epsilon = 0.000002; // The transport uses an integer microsecond timescale.
  let previousEnd = segmentStart;
  for (const sample of samples) {
    if (![sample.media_start_s, sample.media_end_s, sample.frame].every(Number.isFinite) ||
        !Number.isSafeInteger(sample.frame) || sample.frame < 0 ||
        sample.media_start_s < segmentStart - epsilon || sample.media_end_s > segmentEnd + epsilon ||
        sample.media_end_s <= sample.media_start_s || sample.media_start_s < previousEnd - epsilon) return null;
    previousEnd = sample.media_end_s;
  }
  const starts = samples.filter(sample => Math.abs(mediaTime - sample.media_start_s) <= epsilon);
  if (starts.length === 1) return starts[0].frame;
  if (starts.length > 1) return null;
  const matching = samples.filter(sample => mediaTime >= sample.media_start_s && mediaTime < sample.media_end_s);
  return matching.length === 1 ? matching[0].frame : null;
}
