/**
 * Same desk measurement as python -m tools.mac_render.bench.
 * node tools/mac_render/bench.mjs --upstream ws://127.0.0.1:8090
 * Prints JSON. Label is always "Mac desk, no headset".
 */
import process from "node:process";

const args = process.argv.slice(2);
const upstream = (args[args.indexOf("--upstream") + 1] || "ws://127.0.0.1:8090").replace(/^http/, "ws");
const seconds = Number(args[args.indexOf("--seconds") + 1] || 8);

function percentile(values, fraction) {
  const ordered = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!ordered.length) return null;
  const index = (ordered.length - 1) * fraction;
  const low = Math.floor(index);
  const high = Math.ceil(index);
  return ordered[low] + (ordered[high] - ordered[low]) * (index - low);
}

const view = new WebSocket(new URL("/ws/macview", upstream));
const pose = new WebSocket(new URL("/ws/macview-pose", upstream));
const samples = [];
let init = null;
const started = performance.now();

function sendPose() {
  if (pose.readyState !== WebSocket.OPEN) return;
  pose.send(JSON.stringify({
    type: "pose", version: 1,
    sent_mono_ms: performance.now(), sent_wall_ms: Date.now(),
    head: { position: [0, 1.6, 3], quaternion: [0, 0, 0, 1] },
    ray: { origin: [0, 1.6, 3], direction: [0, 0, -1] },
  }));
}

view.onmessage = (event) => {
  if (typeof event.data === "string") {
    const message = JSON.parse(event.data);
    if (message.macview === "macview_init" || message.type === "macview_init") init = message;
    else if (message.macview === "macview_segment" || message.type === "macview_segment") {
      samples.push({
        render_ms: message.render_ms, capture_ms: message.capture_ms, encode_ms: message.encode_ms,
        frame_age_ms: Date.now() - message.capture_wall_ms,
        pose_to_frame_ms: message.pose ? message.capture_mono_ms - message.pose.received_mono_ms : null,
        bytes: 0,
      });
    }
    return;
  }
  if (samples.length) samples[samples.length - 1].bytes = event.data.byteLength || event.data.length || 0;
  if (performance.now() - started > seconds * 1000) {
    const elapsed = (performance.now() - started) / 1000;
    const report = {
      label: "Mac desk, no headset",
      resolution: init ? `${init.width}x${init.height}` : null,
      fps: init?.fps ?? null,
      seconds: elapsed,
      fragments: samples.length,
      stereo: false,
      render_ms_p50: percentile(samples.map((item) => item.render_ms), 0.5),
      capture_ms_p50: percentile(samples.map((item) => item.capture_ms), 0.5),
      encode_ms_p50: percentile(samples.map((item) => item.encode_ms), 0.5),
      bytes_per_s: samples.reduce((sum, item) => sum + item.bytes, 0) / elapsed,
      frame_age_ms_p50: percentile(samples.map((item) => item.frame_age_ms), 0.5),
      frame_age_ms_p95: percentile(samples.map((item) => item.frame_age_ms), 0.95),
      pose_to_frame_ms_p50: percentile(samples.map((item) => item.pose_to_frame_ms), 0.5),
      pose_to_frame_ms_p95: percentile(samples.map((item) => item.pose_to_frame_ms), 0.95),
      note: "Flat pose-driven view. Not stereo. Not a Quest measurement.",
    };
    console.log(JSON.stringify(report, null, 2));
    view.close();
    pose.close();
    return;
  }
  sendPose();
  view.send(JSON.stringify({ type: "next", request_id: samples.length + 1 }));
};
view.onopen = () => view.send(JSON.stringify({ type: "next", request_id: 1 }));
