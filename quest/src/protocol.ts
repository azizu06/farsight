/** Wire shapes from docs/interfaces.md. Do not expand the wire contract here. */
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
}

export interface ControlReply {
  seq: number;
  range_cm: number;
  estop: boolean;
  mode: string;
  link_ms: number;
}
