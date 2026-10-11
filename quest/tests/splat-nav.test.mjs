import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { NAV, SnapTurn, moveStep, sceneFileType, shaped, splatParams, stickAxes, turnAbout, walkInputs, sceneStart } from "../src/splat-nav.js";

const close = (a, b, label) => assert.ok(Math.abs(a - b) < 1e-9, `${label}: ${a} vs ${b}`);

test("left stick walks where the head faces, horizontally, at the configured speed", () => {
  const ahead = moveStep(0, -1, 0, 1);
  close(ahead.dx, 0, "dx"); close(ahead.dz, -0.1 * NAV.moveSpeed, "forward is -Z, dt capped at 0.1 s");
  const right = moveStep(1, 0, 0, 0.05);
  close(right.dx, 0.05 * NAV.moveSpeed, "strafe right is +X"); close(right.dz, 0, "dz");
  const turned = moveStep(0, -1, Math.PI / 2, 0.05); // facing -X after a left yaw of 90 degrees
  close(turned.dx, -0.05 * NAV.moveSpeed, "dx"); close(turned.dz, 0, "dz");
  assert.deepEqual(moveStep(0.1, -0.1, 0, 0.05), { dx: 0, dz: 0 }, "deadzone");
  assert.deepEqual(moveStep(0, -1, 0, NaN), { dx: 0, dz: 0 });
  assert.equal(shaped(NAV.deadzone + 1e-6) > 0, true, "motion starts from zero past the deadzone");
  const diagonal = moveStep(1, -1, 0, .1);
  close(Math.hypot(diagonal.dx, diagonal.dz), .15, "diagonal stays at 1.5 m/s");
});

test("right stick snap-turns once per push and re-arms only after returning", () => {
  const snap = new SnapTurn();
  close(snap.step(0.9), -NAV.snapDeg * Math.PI / 180, "right push turns right (negative yaw)");
  assert.equal(snap.step(0.95), 0, "held: no repeat");
  assert.equal(snap.step(0.5), 0, "still above re-arm");
  assert.equal(snap.step(0.1), 0, "re-armed");
  close(snap.step(-0.8), NAV.snapDeg * Math.PI / 180, "left push turns left");
  assert.equal(new SnapTurn().step(NaN), 0);
});

test("a snap turn spins about the head, not the rig origin", () => {
  const head = { x: 1, z: -2 };
  const rig = turnAbout({ x: 1, z: -2 }, head, Math.PI / 2);
  close(rig.x, 1, "rig at the head stays put"); close(rig.z, -2, "z");
  const offset = turnAbout({ x: 0, z: -2 }, head, Math.PI); // head 1 m right of the rig origin
  close(offset.x, 2, "origin swings to the other side of the head"); close(offset.z, -2, "z");
});

test("xr-standard thumbsticks read axes 2/3, older mappings 0/1", () => {
  assert.deepEqual(stickAxes({ axes: [0, 0, 0.5, -1] }), { x: 0.5, y: -1 });
  assert.deepEqual(stickAxes({ axes: [0.25, 0.75] }), { x: 0.25, y: 0.75 });
  assert.deepEqual(stickAxes(undefined), { x: 0, y: 0 });
});

test("flat standard pads map both sticks; an XR session always owns Touch input", () => {
  const pad = { connected: true, mapping: "standard", axes: [.5, -1, -.8, .1], buttons: [{pressed:true}, {}, {pressed:false}] };
  const flat = walkInputs(null, [null, pad]);
  assert.deepEqual(flat.left, {x:.5,y:-1,reset:false});
  assert.deepEqual(flat.right, {x:-.8,y:.1,reset:true});
  const touch = {axes:[0,0,.7,-.9],buttons:[{},{},{},{},{pressed:true}]};
  assert.deepEqual(walkInputs({inputSources:[{handedness:"left",gamepad:touch}]}, [pad]), {
    left:{x:.7,y:-.9,reset:true},right:{x:0,y:0,reset:false},
  });
  assert.deepEqual(walkInputs(null, [{...touch,id:"Oculus Touch (Left)"}]).left,{x:.7,y:-.9,reset:true});
  assert.equal(walkInputs(null,[{...pad,connected:false}]).left.y,0);
  const rightOnly=walkInputs({inputSources:[{handedness:"right",gamepad:touch}]});
  assert.deepEqual(rightOnly.left,{x:0,y:-.9,reset:false},"right Y walks when left controller is absent");
  assert.deepEqual(rightOnly.right,{x:.7,y:-.9,reset:true},"right X snap and A reset retained");
});

test("start at a captured camera eye and yaw, or inside the room bounds", () => {
  const transforms={frames:[{transform_matrix:[[0,0,-1,2],[0,1,0,1.4],[1,0,0,-3],[0,0,0,1]]}]};
  assert.deepEqual(sceneStart(transforms,null),{eye:[2,1.4,-3],yaw:-Math.PI/2,source:"capture"});
  assert.deepEqual(sceneStart(transforms,null,true),{eye:[2,-1.4,3],yaw:-Math.PI/2,source:"capture"});
  const bounds={min:{x:-2,y:-.4,z:-6},max:{x:4,y:2.6,z:2}};
  const pose=sceneStart(null,bounds);
  close(pose.eye[0],1,"centre x");close(pose.eye[1],1.2,"eye above floor");close(pose.eye[2],-2,"centre z");
  assert.equal(pose.source,"bounds");
  assert.deepEqual(sceneStart(null,null),{eye:[0,1.6,0],yaw:0,source:"default"});
  const path={frames:[
    {transform_matrix:[[1,0,0,2],[0,1,0,1.2],[0,0,1,-3],[0,0,0,1]]},
    {transform_matrix:[[1,0,0,2.1],[0,1,0,1.6],[0,0,1,-3],[0,0,0,1]]},
  ]};
  assert.deepEqual(sceneStart(path,null),{eye:[2,1.4,-3],yaw:0,source:"capture"},"first X/Z and mean path eye height");
});

test("scene URL, format, flip and start pose come from the page URL", () => {
  assert.equal(sceneFileType("/splats/run.PLY?v=2"), "ply");
  assert.equal(sceneFileType("https://x/y.ksplat#a"), "ksplat");
  assert.equal(sceneFileType("/splats/latest", "spz"), "spz", "explicit type for extensionless URLs");
  assert.equal(sceneFileType("/splats/latest"), undefined, "unknown: the loader sniffs");
  const p = splatParams("?scene=%2Fsplats%2Frun1.splat&flip=1&pos=1,0,-2&yaw=90");
  assert.deepEqual(p, { scene: "/splats/run1.splat", type: "splat", flip: true, start: [1, 0, -2], yawDeg: 90 });
  assert.deepEqual(splatParams("?pos=1,2"), { scene: null, type: undefined, flip: false, start: [0, 0, 0], yawDeg: 0 });
});

test("the splat page is its own build entry and never touches rover control", () => {
  const config = readFileSync(new URL("../vite.config.ts", import.meta.url), "utf8");
  assert.match(config, /splat: "splat.html"/);
  const source = readFileSync(new URL("../src/splat-viewer.ts", import.meta.url), "utf8")
    .replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
  assert.doesNotMatch(source, /WebSocket|\/ws\/|rover/i);
  const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.match(pkg.dependencies["@sparkjsdev/spark"], /^\d+\.\d+\.\d+$/, "exact version pin");
});
