// Walkthrough controls and page options for the Gaussian splat viewer (splat.html).
// Pure so the stick handling, snap turn and URL parsing are testable without WebXR.

export const NAV = {
  /** Stick magnitude below this is rest. */
  deadzone: 0.15,
  /** Walking speed at full left-stick deflection (m/s). */
  moveSpeed: 1.5,
  /** One snap turn (degrees). */
  snapDeg: 30,
  /** Right stick |x| that fires a snap turn, and the |x| it must fall below to re-arm. */
  snapOn: 0.7,
  snapOff: 0.3,
};

/** Thumbstick of an XR gamepad: xr-standard puts it on axes 2/3, older mappings on 0/1. */
export function stickAxes(gamepad) {
  const axes = gamepad?.axes ?? [];
  const [x, y] = axes.length >= 4 ? [axes[2], axes[3]] : [axes[0], axes[1]];
  return { x: Number.isFinite(x) ? x : 0, y: Number.isFinite(y) ? y : 0 };
}

/** Read XR controllers, or the pads a nonimmersive browser actually exposes.
 * Touch controllers belong to XRSession.inputSources, not navigator.getGamepads().
 * A regular two-stick gamepad uses the standard mapping (left 0/1, right 2/3).
 */
export function walkInputs(session, gamepads = []) {
  const inputs = { left: { x: 0, y: 0, reset: false }, right: { x: 0, y: 0, reset: false } };
  if (session) {
    let hasLeft = false;
    for (const source of session.inputSources) {
      if (source.gamepad && (source.handedness === "left" || source.handedness === "right")) {
        if (source.handedness === "left") hasLeft = true;
        inputs[source.handedness] = { ...stickAxes(source.gamepad), reset: !!source.gamepad.buttons[4]?.pressed };
      }
    }
    // A single right Touch controller can still explore: Y walks, X snap-turns.
    if (!hasLeft) inputs.left.y = inputs.right.y;
  } else {
    for (const pad of gamepads) {
      if (!pad || pad.connected === false) continue;
      const hand = pad.hand || pad.handedness || String(pad.id).match(/\b(left|right)\b/i)?.[1]?.toLowerCase();
      if (hand === "left" || hand === "right") {
        inputs[hand] = { ...stickAxes(pad), reset: !!pad.buttons[4]?.pressed };
      } else if (pad.mapping === "standard") {
        inputs.left = { ...stickAxes({ axes: pad.axes.slice(0, 2) }), reset: !!pad.buttons[2]?.pressed };
        inputs.right = { ...stickAxes({ axes: pad.axes.slice(2, 4) }), reset: !!pad.buttons[0]?.pressed };
      }
    }
  }
  return inputs;
}

/** Captures use row-major OpenGL camera-to-world matrices in the scene's Y-up metres.
 * Match walk.html's recorded path (5 cm / 0.1 rad samples). Start at its first X/Z
 * and heading, with the mean path camera Y as eye height; bounds are the bare-PLY fallback.
 */
export function sceneStart(transforms, bounds, flip = false) {
  const cameras = [];
  const sign = flip ? -1 : 1;
  for (const frame of transforms?.frames ?? []) {
    const m = frame.transform_matrix;
    if (!Array.isArray(m) || m.length !== 4 || !m.every(row => Array.isArray(row) && row.length === 4 && row.every(Number.isFinite))) continue;
    cameras.push([m[0][3], m[1][3], m[2][3], Math.atan2(m[0][2], sign * m[2][2])]);
  }
  const path = cameras.filter((p, i, a) => !i || Math.hypot(p[0] - a[i-1][0], p[2] - a[i-1][2]) > .05 || Math.abs(p[3] - a[i-1][3]) > .1);
  if (path.length) {
    const first = path[0];
    const eyeY = path.reduce((sum, p) => sum + p[1], 0) / path.length;
    return { eye: [first[0], sign * eyeY, sign * first[2]],
      yaw: first[3], source: "capture" };
  }
  if (bounds && bounds.min.x <= bounds.max.x) {
    const { min, max } = bounds;
    const height = max.y - min.y;
    return { eye: [(min.x + max.x) / 2, height > .3 ? Math.min(min.y + 1.6, max.y - .15) : (min.y + max.y) / 2,
      (min.z + max.z) / 2], yaw: 0, source: "bounds" };
  }
  return { eye: [0, 1.6, 0], yaw: 0, source: "default" };
}

/** Rescale one axis past the deadzone so motion starts smoothly from zero. */
export function shaped(value, deadzone = NAV.deadzone) {
  if (!Number.isFinite(value) || Math.abs(value) <= deadzone) return 0;
  return Math.sign(value) * Math.min(1, (Math.abs(value) - deadzone) / (1 - deadzone));
}

/**
 * Horizontal walk step in world space for one frame. Stick up walks where the head
 * faces (yaw only, so looking down never sinks you through the floor).
 * @param {number} x left stick x @param {number} y left stick y (up is negative)
 * @param {number} headYaw radians, world yaw of the head @param {number} dt seconds
 */
export function moveStep(x, y, headYaw, dt, speed = NAV.moveSpeed) {
  const strafe = shaped(x), forward = -shaped(y);
  if (!strafe && !forward || !(dt > 0)) return { dx: 0, dz: 0 };
  const step = Math.min(dt, 0.1) * speed / Math.max(1, Math.hypot(strafe, forward));
  const sin = Math.sin(headYaw), cos = Math.cos(headYaw);
  // three.js: yaw 0 faces -Z; +X is to the right.
  return { dx: (strafe * cos - forward * sin) * step, dz: (-strafe * sin - forward * cos) * step };
}

/** Right-stick snap turn with hysteresis: one turn per push, re-armed only after the stick returns. */
export class SnapTurn {
  constructor() { this.armed = true; }
  /** @returns {number} turn in radians this frame (positive turns left, as three.js yaw) */
  step(x) {
    const value = Number.isFinite(x) ? x : 0;
    if (!this.armed) {
      if (Math.abs(value) < NAV.snapOff) this.armed = true;
      return 0;
    }
    if (Math.abs(value) < NAV.snapOn) return 0;
    this.armed = false;
    return -Math.sign(value) * NAV.snapDeg * Math.PI / 180;
  }
}

const TYPES = new Set(["ply", "splat", "ksplat", "spz"]);

/** Splat format from an explicit `type=` or the URL's file extension; undefined lets the loader sniff. */
export function sceneFileType(url, override) {
  const forced = typeof override === "string" ? override.toLowerCase() : "";
  if (TYPES.has(forced)) return forced;
  const path = String(url ?? "").split(/[?#]/)[0].toLowerCase();
  const extension = path.includes(".") ? path.slice(path.lastIndexOf(".") + 1) : "";
  return TYPES.has(extension) ? extension : undefined;
}

/**
 * splat.html?scene=<url>[&type=ply|splat|ksplat|spz][&flip=1][&pos=x,y,z][&yaw=deg]
 * `flip` turns a Y-down (COLMAP/3DGS training) scene upright. `pos`/`yaw` place the
 * start: the viewer's floor point and heading inside the scene.
 */
export function splatParams(search) {
  const params = new URLSearchParams(search);
  const scene = params.get("scene")?.trim() || null;
  const pos = (params.get("pos") ?? "").split(",").map(Number);
  const yaw = Number(params.get("yaw"));
  return {
    scene,
    type: sceneFileType(scene, params.get("type")),
    flip: params.get("flip") === "1",
    start: pos.length === 3 && pos.every(Number.isFinite) ? pos : [0, 0, 0],
    yawDeg: Number.isFinite(yaw) ? yaw : 0,
  };
}

/**
 * Turn the rig by `angle` about a vertical axis through the head, so a snap turn
 * spins you in place instead of swinging you around the rig origin.
 * @param {{x:number, z:number}} rig rig position @param {{x:number, z:number}} head head world position
 */
export function turnAbout(rig, head, angle) {
  const sin = Math.sin(angle), cos = Math.cos(angle);
  const ox = rig.x - head.x, oz = rig.z - head.z;
  return { x: head.x + ox * cos + oz * sin, z: head.z - ox * sin + oz * cos };
}
