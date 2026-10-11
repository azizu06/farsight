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
  const step = Math.min(dt, 0.1) * speed;
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
