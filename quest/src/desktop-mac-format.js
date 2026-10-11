export function macFormat(message) {
  if (message.version !== 1) return null;
  if (message.stereo === false) return { stereo: false };
  if (message.stereo !== true || message.layout !== "sbs-left-right" ||
      !Number.isSafeInteger(message.eye_width) || message.eye_width <= 0 ||
      !Number.isSafeInteger(message.eye_height) || message.eye_height <= 0) return null;
  if (message.type === "video_init" &&
      (message.width !== message.eye_width * 2 || message.height !== message.eye_height)) return null;
  if (message.type === "video_segment") {
    const eyes = message.pose?.eyes;
    if (!Array.isArray(eyes) || eyes.length !== 2 || eyes[0].eye !== "left" || eyes[1].eye !== "right") return null;
    for (const eye of eyes) for (const [key, size] of [["position", 3], ["quaternion", 4], ["projection", 16]])
      if (!Array.isArray(eye[key]) || eye[key].length !== size || !eye[key].every(Number.isFinite)) return null;
  }
  return { stereo: true, eyeWidth: message.eye_width, eyeHeight: message.eye_height };
}

// Preserve image proportions; SBS previews show the actual left eye, never squeezed pairs.
export function macRectangle(width, height, canvasWidth, canvasHeight, stereo) {
  const sourceWidth = stereo ? width / 2 : width;
  const scale = Math.min(canvasWidth / sourceWidth, canvasHeight / height);
  return { sourceWidth, width: sourceWidth * scale, height: height * scale,
    x: (canvasWidth - sourceWidth * scale) / 2, y: (canvasHeight - height * scale) / 2 };
}
