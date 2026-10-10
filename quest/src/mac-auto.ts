/**
 * Mac render inside AUTO (`?macview=1`): the Mac draws the AR map from the headset pose
 * mapped into map coordinates and streams H.264 on /ws/macview; this shows it as a flat
 * picture in the same page, under the same RoverExperience and its single /ws/control.
 * Display only: no control socket, no motion. Flat, not stereo, and labelled so.
 */
import * as THREE from "three";
import { H264Viewer } from "./h264.js";
import { mapWalk } from "./map-walk.js";
import { socketUrl } from "./mac-view-urls.js";
import {
  DEFAULT_ASPECT, DEFAULT_FOV_Y_DEG, PICTURE_DISTANCE, PoseRing, macPresenting, mapPoseMessage, pictureSize, shouldSendPose,
} from "./mac-auto-pose.js";

export const MACVIEW_PATH = "/ws/macview";
export const MACVIEW_POSE_PATH = "/ws/macview-pose";
const LABEL = "MAC RENDER · FLAT";
const LOST = "MAC VIEW LOST · LOCAL MAP";

type WorldPose = { id: number; mono: number; position: number[]; quaternion: number[] };

export class MacAutoView {
  readonly group = new THREE.Group();
  readonly viewer: H264Viewer;
  private readonly texture: THREE.VideoTexture;
  private readonly picture: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>;
  private readonly label: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>;
  private readonly labelCanvas = document.createElement("canvas");
  private readonly labelTexture: THREE.CanvasTexture;
  private labelText = "";
  private poseSocket: WebSocket | null = null;
  private retry = 0;
  private active = false;
  private session = "";
  private poseId = 0;
  private lastPoseAt = -Infinity;
  private lastFrameAt = -Infinity;
  private readonly ring = new PoseRing();
  private readonly head = { position: new THREE.Vector3(), quaternion: new THREE.Quaternion() };
  private readonly eye = { position: new THREE.Vector3(), quaternion: new THREE.Quaternion(), scale: new THREE.Vector3() };
  private readonly offset = new THREE.Vector3();
  /** True while a fresh Mac picture replaces the local map. */
  presenting = false;

  constructor(private renderer: THREE.WebGLRenderer, scene: THREE.Scene, private mapRevision: () => number | null) {
    this.viewer = new H264Viewer(() => {}, () => {}, MACVIEW_PATH);
    this.texture = new THREE.VideoTexture(this.viewer.video);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.picture = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ map: this.texture, toneMapped: false, depthWrite: false }));
    this.picture.renderOrder = -1;
    this.labelCanvas.width = 512; this.labelCanvas.height = 64;
    this.labelTexture = new THREE.CanvasTexture(this.labelCanvas);
    this.labelTexture.colorSpace = THREE.SRGBColorSpace;
    this.label = new THREE.Mesh(new THREE.PlaneGeometry(0.48, 0.06), new THREE.MeshBasicMaterial({ map: this.labelTexture, transparent: true, depthTest: false, toneMapped: false }));
    this.label.renderOrder = 10;
    this.group.add(this.picture, this.label);
    this.group.visible = false;
    scene.add(this.group);
  }

  /** Call once per frame before rendering. Returns whether the Mac picture replaces the local map. */
  update(now: number, active: boolean) {
    if (active !== this.active) { if (active) this.start(); else this.stop(); }
    if (!active) return false;
    const xrCamera = this.renderer.xr.getCamera();
    xrCamera.getWorldPosition(this.head.position); xrCamera.getWorldQuaternion(this.head.quaternion);
    this.sendPose(now, xrCamera);
    this.viewer.update();
    const shown = this.viewer.displayedFrame;
    if (shown) { this.lastFrameAt = now; this.place(shown.pose, shown.fovY); }
    this.presenting = macPresenting(true, this.lastFrameAt, now);
    this.picture.visible = this.presenting;
    this.paintLabel(this.presenting ? LABEL : LOST);
    // The label stays readable just under the gaze, independent of the picture's lag.
    this.label.position.copy(this.offset.set(0, -0.32, -1.5).applyQuaternion(this.head.quaternion).add(this.head.position));
    this.label.quaternion.copy(this.head.quaternion);
    this.group.visible = true;
    return this.presenting;
  }

  private start() {
    this.active = true;
    this.session = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    this.lastFrameAt = -Infinity; this.lastPoseAt = -Infinity; this.ring.clear();
    this.connectPose(); this.viewer.start();
  }

  stop() {
    this.active = false; this.presenting = false; this.group.visible = false;
    window.clearTimeout(this.retry);
    if (this.poseSocket) { this.poseSocket.onclose = null; this.poseSocket.close(); this.poseSocket = null; }
    this.viewer.stop();
  }

  private connectPose() {
    if (!this.active) return;
    const socket = new WebSocket(socketUrl(location.origin, MACVIEW_POSE_PATH));
    this.poseSocket = socket;
    socket.onclose = () => { if (this.poseSocket === socket) { this.poseSocket = null; this.retry = window.setTimeout(() => this.connectPose(), 1000); } };
  }

  private sendPose(now: number, xrCamera: THREE.ArrayCamera) {
    const socket = this.poseSocket;
    if (socket?.readyState !== WebSocket.OPEN || !shouldSendPose(this.lastPoseAt, now, socket.bufferedAmount)) return;
    const eyes = xrCamera.cameras.slice(0, 2).map(camera => {
      camera.matrixWorld.decompose(this.eye.position, this.eye.quaternion, this.eye.scale);
      return { position: this.eye.position.toArray(), quaternion: this.eye.quaternion.toArray(), projection: [...camera.projectionMatrix.elements] };
    });
    const id = ++this.poseId;
    const position = this.head.position.toArray(), quaternion = this.head.quaternion.toArray();
    const message = mapPoseMessage({
      id, mono: now, wall: Date.now(), walk: mapWalk, head: { position, quaternion }, ray: null, eyes,
      session: this.session, mapRevision: this.mapRevision(), aspect: DEFAULT_ASPECT,
    });
    if (!message) return;
    this.lastPoseAt = now;
    this.ring.push({ id, mono: now, position, quaternion } satisfies WorldPose);
    socket.send(JSON.stringify(message));
  }

  /** Put the picture where it was rendered from (flat reprojection), else in front of the head. */
  private place(echo: Record<string, unknown> | null, fovY: number | undefined) {
    const from = this.ring.find(echo) as WorldPose | null;
    const position = from ? this.offset.fromArray(from.position) : this.offset.copy(this.head.position);
    const quaternion = from ? this.picture.quaternion.fromArray(from.quaternion) : this.picture.quaternion.copy(this.head.quaternion);
    const video = this.viewer.video;
    const aspect = video.videoWidth > 0 && video.videoHeight > 0 ? video.videoWidth / video.videoHeight : DEFAULT_ASPECT;
    const { width, height } = pictureSize(fovY ?? DEFAULT_FOV_Y_DEG, aspect);
    this.picture.scale.set(width, height, 1);
    this.picture.position.set(0, 0, -PICTURE_DISTANCE).applyQuaternion(quaternion).add(position);
  }

  private paintLabel(text: string) {
    if (text === this.labelText) return;
    this.labelText = text;
    const context = this.labelCanvas.getContext("2d");
    if (!context) return;
    const lost = text === LOST;
    context.clearRect(0, 0, 512, 64);
    context.fillStyle = lost ? "rgba(120, 60, 0, 0.85)" : "rgba(8, 15, 22, 0.75)";
    context.beginPath(); context.roundRect(4, 4, 504, 56, 28); context.fill();
    context.fillStyle = lost ? "#ffd27a" : "#9fe7ff";
    context.font = "600 26px system-ui, sans-serif"; context.textAlign = "center"; context.textBaseline = "middle";
    context.fillText(text, 256, 33);
    this.labelTexture.needsUpdate = true;
  }
}
