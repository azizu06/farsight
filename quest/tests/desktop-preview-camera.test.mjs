import test from "node:test";
import assert from "node:assert/strict";
import * as THREE from "three";
import { DesktopPreviewCamera } from "../../tools/mac_render/desktop-preview.mjs";
import { PoseRing, frameView } from "../src/mac-auto-pose.js";
import { macFormat } from "../src/desktop-mac-format.js";

test("desktop eyes use the retained camera, packed-eye aspect and a distinct sequential identity", () => {
  const camera = new THREE.PerspectiveCamera(50, 16 / 9, .01, 2000);
  camera.position.set(9, 6, 9); camera.lookAt(0, 0, 0);
  const preview = new DesktopPreviewCamera({ THREE, camera, width: 1920, height: 1080, session: "desktop-preview-test" });
  const first = preview.next(), second = preview.next();
  assert.equal(first.desktop_preview, true);
  assert.equal(second.frame_id, first.frame_id + 1);
  assert.equal(second.session_id, first.session_id);
  assert.deepEqual(first.eyes.map(eye => eye.eye), ["left", "right"]);
  assert.ok(Math.abs(new THREE.Vector3(...first.eyes[0].position).distanceTo(new THREE.Vector3(...first.eyes[1].position)) - .064) < 1e-12);
  const midpoint = new THREE.Vector3(...first.eyes[0].position).add(new THREE.Vector3(...first.eyes[1].position)).multiplyScalar(.5);
  assert.ok(midpoint.distanceTo(camera.position) < 1e-12);
  const metadata = { type: "video_segment", version: 1, stereo: true, layout: "sbs-left-right", eye_width: 960, eye_height: 1080, pose: first };
  assert.ok(macFormat(metadata), "desktop decoder can inspect the actual packed fallback");
  assert.equal(frameView(metadata, first, new PoseRing(), "headset-session"), null, "headset cannot mistake desk eyes for tracking");
});

test("a marked desktop frame cannot enter XR even with a matching session", () => {
  assert.equal(frameView({ version: 1, stereo: false }, { session_id: "headset", desktop_preview: true }, new PoseRing(), "headset"), null);
});
