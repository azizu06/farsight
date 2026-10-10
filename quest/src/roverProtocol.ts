/** Wire shapes from docs/interfaces.md and jetson/control/state.py. */
export interface DetectionBox {
  id: number;
  cls: string;
  conf: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface DetectionPacket {
  ts: number;
  frame: number;
  fps: number;
  boxes: DetectionBox[];
}

export interface ControlCommand {
  seq: number;
  deadman: boolean;
  v: number;
  w: number;
  pan: number;
  mode: "teleop";
  session_id: string;
  permit: string | null;
  video_fresh: boolean;
  video_age_ms: number;
}

export interface ControlReply {
  seq: number;
  range_cm: number | null;
  estop: boolean;
  mode: string;
  link_ms: number | null;
  // Optional here for legacy HUD packet readers. ControlLink requires the
  // complete authorization status before it can send a held command.
  type?: "control_status";
  session_id?: string | null;
  permit?: string | null;
  permit_ms?: number;
  rover?: boolean;
  simulated?: boolean;
  requires_release?: boolean;
  reason?: string;
  left_pwm?: number;
  right_pwm?: number;
  pan_deg?: number;
  max_pwm?: number;
}
