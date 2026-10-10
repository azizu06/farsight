/** One short cause plus the one action that recovers it, for every stop the
 * server reports (control_status `reason`) and every client latch. Display only:
 * nothing here gates motion; ControlSafety and the server still decide. */

const RELEASE = "release";
const WAIT = "wait";

/** Server `reason` codes from jetson/control (and the Rust port). [cause, recovery] */
const SERVER = {
  release_required: ["Control re-armed", RELEASE],
  deadman_released: ["Deadman released", RELEASE],
  lease_expired: ["Command lease expired (link lag > 250 ms)", "Check Wi-Fi/USB link, then release and hold {deadman}"],
  expired_permit: ["Control permit expired (link lag)", "Check Wi-Fi/USB link, then release and hold {deadman}"],
  stale_sequence: ["Out-of-order command (link lag)", RELEASE],
  invalid_sequence: ["Malformed command sequence", "Reload viewer, then release and hold {deadman}"],
  wrong_session: ["Old control session", RELEASE],
  controller_disconnected: ["Previous controller disconnected", RELEASE],
  camera_stale: ["Rover camera stale", "Wait for live video, then release and hold {deadman}"],
  video_stale: ["Viewer video stale (> 500 ms)", "Wait for live video, then release and hold {deadman}"],
  serial_disconnected: ["Motor board (Uno) disconnected", "Check Uno USB and battery; release and hold when back"],
  rover_disabled: ["Motors disabled on server (no serial port)", "Restart server with --serial PORT (or --simulate)"],
  shutdown: ["Rover server shutting down", "Restart the rover server, then reload viewer"],
  uno_rejected_command: ["Motor board rejected a command", "Release and hold {deadman}; check Uno firmware if repeated"],
  scan_finished: ["Scan finished", RELEASE],
  scan_override: ["Stick moved during scan", "Center sticks; press Y to scan again"],
  scan_range_stop: ["Scan stopped: range lost or obstacle", "Clear the front, then press Y to scan again"],
  scan_not_active: ["Scan not reserved", "Press Y to arm scan again"],
  autonomy_unavailable: ["Autonomy lost camera or range", "Wait for fresh camera/range, then release and hold {deadman}"],
  unsupported_mode: ["Rover rejected drive mode", "Reload viewer, then release and hold {deadman}"],
  invalid_target: ["Rover rejected seek target", "Pick the person again, then release and hold {deadman}"],
  invalid_command: ["Rover rejected command values", "Reload viewer, then release and hold {deadman}"],
  invalid_json: ["Rover rejected malformed command", "Reload viewer, then release and hold {deadman}"],
  binary_control_rejected: ["Rover rejected binary command", "Reload viewer, then release and hold {deadman}"],
  oversized_command: ["Rover rejected oversized command", "Reload viewer, then release and hold {deadman}"],
};

/** ControlSafety.readiness() results: motion blocked until the condition clears. */
const UNSAFE = {
  "Control disconnected": ["Control link down", "Check rover Wi-Fi/USB; reconnects every 1 s"],
  "XR input stale: release trigger": ["Controller input stale", "Keep controllers in view; release and hold {deadman}"],
  "Control status stale": ["No rover status for 300 ms", "Check Wi-Fi/USB link; wait for status"],
  "Rover unavailable": ["Motor board not connected", "Check Uno USB cable and battery"],
  "Camera stale: motion stopped": ["Camera stale (> 500 ms)", "Wait for live video; check camera or phone link"],
  "Waiting for fresh control permit": ["Waiting for control permit", WAIT],
  // Set by ControlSafety.decide() on every tick the viewer cannot take input.
  "Input or display inactive: release deadman": ["Viewer not accepting input", "Close dialogs, back to camera view; release, hold {deadman}"],
};

/** Client latches whose text has no "cause: recovery" form, or needs a better action. */
const CLIENT = {
  "Waiting for live camera and rover": ["Waiting for live camera and rover", WAIT],
  "Release deadman to continue": ["Deadman must be released", RELEASE],
  "Rover unavailable": ["Motor board not connected", "Check Uno USB and battery; release and hold when back"],
  "Invalid control status": ["Bad status from rover", "Reload viewer"],
  "Viewer closed": ["Viewer closed", "Reopen the viewer"],
  "Scan status unavailable": ["Scan status lost", "Press Y to scan again"],
  "XR tracking unavailable: release trigger": ["Controller tracking lost", "Bring both controllers into view; release and hold {deadman}"],
  "Voice question: release trigger": ["Listening to voice question", "Finish the question; release and hold {deadman}"],
  "Control disconnected: release deadman": ["Control link dropped", "Check rover Wi-Fi/USB; release and hold {deadman}"],
  "Control queue stalled: release deadman": ["Control link backed up", "Check Wi-Fi/USB link; release and hold {deadman}"],
  "Browser paused: release deadman": ["Viewer paused (> 200 ms)", RELEASE],
};

const AUTONOMY = {
  target_missing: ["Seek target not visible", "Turn camera toward the person, or take the stick"],
  target_lost: ["Seek target lost", "Point at the person and press A again"],
  estop: ["Autonomy stopped by front range", "Take the stick; reverse or turn"],
};

function recovery(action, xr) {
  const deadman = xr ? "right trigger" : "Space (or Hold)";
  if (action === RELEASE) return `Release, then hold ${deadman}`;
  if (action === WAIT) return "Wait; motion resumes automatically when fresh";
  return action.replaceAll("{deadman}", deadman);
}

function entry(code, [cause, action], xr, tone = "stop") {
  return { code, cause, recovery: recovery(action, xr), tone };
}

/** Free text of the form "Cause: release deadman" (or other recovery text). */
function fromClientText(text, xr) {
  if (CLIENT[text]) return entry(text, CLIENT[text], xr);
  const split = text.indexOf(": ");
  if (split < 0) return entry(text, [text, RELEASE], xr);
  const cause = text.slice(0, split), rest = text.slice(split + 2);
  return entry(text, [cause, /^release (deadman|trigger)$/i.test(rest) || /motion disabled/i.test(rest) ? RELEASE : rest], xr);
}

/** Explain the current stop for the operator, or null when nothing needs explaining.
 * `reason` is ControlSafety's current reason; it counts as unsafe only when it names a live condition.
 * @param {{ unsafe?: string | null, reason?: string | null, latched?: boolean, cause?: string | null,
 *   status?: { reason?: string | null, requires_release?: boolean, estop?: boolean,
 *     autonomy_state?: string, target_id?: number | null } | null,
 *   xr?: boolean, held?: boolean, autonomyArmed?: boolean, detectorStale?: boolean }} state
 * @returns {{ code: string, cause: string, recovery: string, tone: "stop" | "warn" } | null}
 */
export function explainStop({ unsafe = null, reason: current = null, latched = false, cause = null, status = null, xr = false,
  held = false, autonomyArmed = false, detectorStale = false } = {}) {
  if (!unsafe && current && UNSAFE[current]) unsafe = current;
  if (unsafe) return UNSAFE[unsafe] ? entry(unsafe, UNSAFE[unsafe], xr) : fromClientText(unsafe, xr);
  if (latched) {
    const reason = cause ?? (status?.requires_release ? status.reason : null) ?? "Release deadman to continue";
    if (SERVER[reason]) return entry(reason, SERVER[reason], xr);
    // The unsafe condition has cleared; only the release latch remains.
    if (UNSAFE[reason] && !CLIENT[reason]) return entry(reason, [UNSAFE[reason][0], RELEASE], xr);
    return fromClientText(reason, xr);
  }
  const auto = AUTONOMY[status?.autonomy_state ?? ""];
  if (auto) {
    const who = Number.isSafeInteger(status?.target_id) ? ` #${status.target_id}` : "";
    return entry(status.autonomy_state, [auto[0].replace("target", `target${who}`), auto[1]], xr, "warn");
  }
  if (held && status?.estop)
    return entry("forward_blocked", ["Forward blocked: obstacle < 25 cm, range unknown, or pan off-center", xr ? "Reverse or turn; face forward or press X to center pan" : "Reverse or turn away from the obstacle"], xr, "warn");
  if (autonomyArmed && detectorStale)
    return entry("detector_stale", ["Detector slow: no fresh detections", "Drive manually, or wait for detections"], xr, "warn");
  return null;
}

export const STOP_CODES = { server: Object.keys(SERVER), unsafe: Object.keys(UNSAFE), client: Object.keys(CLIENT), autonomy: Object.keys(AUTONOMY) };
