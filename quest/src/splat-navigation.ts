import * as THREE from "three";
import type { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { SnapTurn, moveStep, turnAbout, walkInputs } from "./splat-nav.js";

/** XR moves the camera's rig; flat view moves the camera and OrbitControls' world target. */
export class SplatNavigation {
  private start = { eye: [0, 1.6, 0], yaw: 0 };
  private snap = new SnapTurn();
  private resetHeld = false;
  private anchorPending = false;
  private anchorPose = this.start;
  private localHead = new THREE.Vector3(0, 1.6, 0);
  private localOrientation = new THREE.Quaternion();
  private head = new THREE.Vector3();
  private orientation = new THREE.Quaternion();
  private euler = new THREE.Euler(0, 0, 0, "YXZ");
  inputs = walkInputs(null);

  constructor(private renderer: THREE.WebGLRenderer, private rig: THREE.Group,
    private camera: THREE.PerspectiveCamera, private controls: OrbitControls) {}

  setStart(start: { eye: number[]; yaw: number }) { this.start = start; }

  reset() { this.teleport(this.start); }

  /** Place the tracked eye at a capture pose without changing A/X's original start. */
  teleport(pose: { eye: number[]; yaw: number }) {
    this.anchorPose = pose;
    this.rig.rotation.set(0, pose.yaw, 0);
    this.rig.position.fromArray(pose.eye);
    if (this.renderer.xr.isPresenting) {
      // Anchor the actual tracked eye, including room-scale X/Z and seated height.
      this.rig.rotation.y -= this.euler.setFromQuaternion(this.localOrientation, "YXZ").y;
      this.rig.position.sub(this.localHead.clone().applyQuaternion(this.rig.quaternion));
      this.anchorPending = true;
    } else {
      // OrbitControls assumes world-space camera position/target: keep its parent identity.
      this.rig.position.set(0, 0, 0); this.rig.rotation.set(0, 0, 0);
      this.rig.updateMatrixWorld(true);
      this.camera.position.fromArray(pose.eye);
      this.camera.rotation.set(0, pose.yaw, 0);
      this.controls.target.copy(this.camera.position).add(new THREE.Vector3(0, 0, -1).applyQuaternion(this.camera.quaternion));
      const damping = this.controls.enableDamping;
      this.controls.enableDamping = false;
      this.controls.update();
      this.controls.enableDamping = damping;
    }
    this.rig.updateMatrixWorld(true);
  }

  update(dt: number, frame?: XRFrame, gamepads: (Gamepad | null)[] = []) {
    const session = this.renderer.xr.getSession();
    if (session && frame) {
      const reference = this.renderer.xr.getReferenceSpace();
      const pose = reference && frame.getViewerPose(reference);
      if (pose) {
        this.localHead.copy(pose.transform.position);
        this.localOrientation.copy(pose.transform.orientation);
        if (this.anchorPending) {
          this.rig.rotation.y = this.anchorPose.yaw - this.euler.setFromQuaternion(this.localOrientation, "YXZ").y;
          this.rig.position.fromArray(this.anchorPose.eye).sub(this.localHead.clone().applyQuaternion(this.rig.quaternion));
          this.anchorPending = false;
        }
      }
    }
    this.rig.updateMatrixWorld(true);
    if (session) {
      // Read this XRFrame, not the XR camera's previous-render world matrix.
      this.head.copy(this.localHead).applyMatrix4(this.rig.matrixWorld);
      this.orientation.copy(this.rig.quaternion).multiply(this.localOrientation);
    } else {
      this.camera.getWorldPosition(this.head);
      this.camera.getWorldQuaternion(this.orientation);
    }
    this.euler.setFromQuaternion(this.orientation, "YXZ");
    this.inputs = walkInputs(session, gamepads);
    const { left, right } = this.inputs;
    const pressed = left.reset || right.reset;
    if (pressed && !this.resetHeld) this.reset();
    this.resetHeld = pressed;
    if (pressed) { this.snap.step(right.x); return; }

    const { dx, dz } = moveStep(left.x, left.y, this.euler.y, dt);
    const turn = this.snap.step(right.x);
    if (session) {
      this.rig.position.x += dx; this.rig.position.z += dz;
      this.head.x += dx; this.head.z += dz;
      if (turn) {
        const next = turnAbout(this.rig.position, this.head, turn);
        this.rig.position.x = next.x; this.rig.position.z = next.z;
        this.rig.rotation.y += turn;
      }
    } else {
      this.camera.position.x += dx; this.camera.position.z += dz;
      this.controls.target.x += dx; this.controls.target.z += dz;
      if (turn) {
        const offset = this.controls.target.clone().sub(this.camera.position);
        offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), turn);
        this.controls.target.copy(this.camera.position).add(offset);
        this.controls.update();
      }
    }
    this.rig.updateMatrixWorld(true);
  }
}
