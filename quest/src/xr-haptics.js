/**
 * Short controller buzz to confirm a button action in VR. Uses the WebXR gamepad's
 * haptic actuator (Quest Browser), else the Gamepad vibration actuator; silently
 * does nothing where neither exists.
 * @param {Gamepad | null | undefined} gamepad @returns {boolean} whether a pulse was requested
 */
export function hapticPulse(gamepad, intensity = 0.6, ms = 60) {
  try {
    const actuator = /** @type {any} */ (gamepad)?.hapticActuators?.[0];
    if (typeof actuator?.pulse === "function") { void actuator.pulse(intensity, ms); return true; }
    const vibration = /** @type {any} */ (gamepad)?.vibrationActuator;
    if (typeof vibration?.playEffect === "function") {
      void vibration.playEffect("dual-rumble", { duration: ms, strongMagnitude: intensity, weakMagnitude: intensity });
      return true;
    }
  } catch { /* haptics are a courtesy; never affect control */ }
  return false;
}
