// Virtual movement only. The exploration page has no rover control connection.
const DEADZONE = .18;
export const EXPLORE_SPEED = 1.4;
export const EXPLORE_TURN = Math.PI / 6;
export function exploreAxis(value = 0) {
  return Math.sign(value) * Math.max(0, (Math.abs(value) - DEADZONE) / (1 - DEADZONE));
}

export class ExploreNavigation {
  constructor(walk) { this.walk = walk; this.reset(); }
  reset() { this.forward = this.right = this.up = 0; this.turnArmed = true; this.a = this.b = false; }
  /** Standard WebXR thumbsticks occupy axes 2/3; two-axis devices use 0/1. */
  update(dt, sources = [], keys = new Set()) {
    const pad = hand => sources.find(source => source.handedness === hand)?.gamepad;
    const axis = (gamepad, at) => gamepad?.axes?.[gamepad.axes.length >= 4 ? at + 2 : at] ?? 0;
    const left = pad('left'), right = pad('right');
    let forward = -exploreAxis(axis(left, 1)) + Number(keys.has('KeyW')) - Number(keys.has('KeyS'));
    let strafe = exploreAxis(axis(left, 0)) + Number(keys.has('KeyD')) - Number(keys.has('KeyA'));
    const length = Math.max(1, Math.hypot(forward, strafe));
    forward /= length; strafe /= length;
    const up = -exploreAxis(axis(right, 1)) + Number(keys.has('KeyE')) - Number(keys.has('KeyQ'));
    // A short ease-in/out is purely visual; the headset's tracked pose stays immediate.
    const blend = 1 - Math.exp(-dt / .1);
    this.forward += (forward - this.forward) * blend;
    this.right += (strafe - this.right) * blend;
    this.up += (up - this.up) * blend;
    const x = axis(right, 0);
    if (Math.abs(x) < .25) this.turnArmed = true;
    const turn = this.turnArmed && Math.abs(x) > .7 ? -Math.sign(x) * EXPLORE_TURN : 0;
    if (turn) this.turnArmed = false;
    this.walk.moveViewer(this.forward * EXPLORE_SPEED * dt, this.right * EXPLORE_SPEED * dt, this.up * .8 * dt, turn);
    const a = right?.buttons?.[4]?.pressed ?? false, b = right?.buttons?.[5]?.pressed ?? false;
    const actions = { help: a && !this.a, home: b && !this.b };
    this.a = a; this.b = b;
    return actions;
  }
}
