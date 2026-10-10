/**
 * Mac render inside AUTO (`?macview=1`): the Mac draws the AR map from the headset pose
 * mapped into map coordinates and streams H.264 on /ws/macview; this shows it as a flat
 * picture in the same page, under the same RoverExperience and its single /ws/control.
 * Display only: no control socket, no motion. Flat by default and labelled so; with
 * `?stereo=1` it asks for packed side-by-side stereo and shows each half to its own
 * eye (three.js XR camera layers 1/2), only while segments declare a valid pair.
 */
import * as THREE from "three";
import { H264Viewer } from "./h264.js";
import { mapWalk } from "./map-walk.js";
import { socketUrl } from "./mac-view-urls.js";
import {
  DEFAULT_ASPECT, DEFAULT_FOV_Y_DEG, PICTURE_DISTANCE, PoseRing, STEREO_LAYOUT, frameSeenAt, frustumRect, macPresenting, mapPoseMessage,
  pictureSize, segmentKind, shouldSendPose, viewerEyes,
} from "./mac-auto-pose.js";

export const MACVIEW_PATH = "/ws/macview";
export const MACVIEW_POSE_PATH = "/ws/macview-pose";
const LABEL = "MAC RENDER · FLAT";
const STEREO_LABEL = "MAC RENDER · STEREO";
type EyePose = { position: number[]; quaternion: number[]; projection: number[] };
const LOST = "MAC VIEW LOST · LOCAL MAP";

type WorldPose = { id: number; mono: number; position: number[]; quaternion: number[]; eyes: EyePose[] };

/** A unit quad showing one half of the side-by-side frame, visible only to that XR eye. */
function eyeQuad(texture: THREE.Texture, half: 0 | 1) {
  const geometry = new THREE.PlaneGeometry(1, 1);
  const uv = geometry.getAttribute("uv");
  for (let i = 0; i < uv.count; i++) uv.setX(i, half * 0.5 + uv.getX(i) * 0.5);
  const mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ map: texture, toneMapped: false, depthWrite: false }));
  mesh.renderOrder = -1;
  mesh.layers.set(half + 1); // WebXRManager renders layer 1 to the left eye, layer 2 to the right
  mesh.visible = false;
  return mesh;
}

export class MacAutoView {
  readonly group = new THREE.Group();
  readonly viewer: H264Viewer;
  private readonly texture: THREE.VideoTexture;
  private readonly picture: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>;
  private readonly eyeQuads: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>[];
  private stereoShown = false;
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
  private lastFrameId: string | null = null;
  private readonly ring = new PoseRing();
  private readonly head = { position: new THREE.Vector3(), quaternion: new THREE.Quaternion() };
  private readonly offset = new THREE.Vector3();
  /** True while a fresh Mac picture replaces the local map. */
  presenting = false;

  constructor(private renderer: THREE.WebGLRenderer, scene: THREE.Scene, private mapRevision: () => number | null,
              readonly stereo = false) {
    this.viewer = new H264Viewer(() => {}, () => {}, stereo ? `${MACVIEW_PATH}?layout=${STEREO_LAYOUT}` : MACVIEW_PATH);
    this.texture = new THREE.VideoTexture(this.viewer.video);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.picture = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ map: this.texture, toneMapped: false, depthWrite: false }));
    this.picture.renderOrder = -1;
    this.labelCanvas.width = 512; this.labelCanvas.height = 64;
    this.labelTexture = new THREE.CanvasTexture(this.labelCanvas);
    this.labelTexture.colorSpace = THREE.SRGBColorSpace;
    this.label = new THREE.Mesh(new THREE.PlaneGeometry(0.48, 0.06), new THREE.MeshBasicMaterial({ map: this.labelTexture, transparent: true, depthTest: false, toneMapped: false }));
    this.label.renderOrder = 10;
    this.eyeQuads = [eyeQuad(this.texture, 0), eyeQuad(this.texture, 1)];
    this.group.add(this.picture, this.label, ...this.eyeQuads);
    this.group.visible = false;
    scene.add(this.group);
  }

  /** Call once per frame before rendering. Returns whether the Mac picture replaces the local map. */
  update(now: number, active: boolean) {
    if (active !== this.active) { if (active) this.start(); else this.stop(); }
    if (!active) return false;
    // Poses come from this XRFrame: the XR camera's matrixWorld is only refreshed inside
    // renderer.render, so before render it is a frame old. Reference space is world
    // space here because the camera has no parent.
    const space = this.renderer.xr.getReferenceSpace();
    const viewer = space ? this.renderer.xr.getFrame()?.getViewerPose(space) : undefined;
    if (viewer) {
      const { position: p, orientation: q } = viewer.transform;
      this.head.position.set(p.x, p.y, p.z); this.head.quaternion.set(q.x, q.y, q.z, q.w);
    } else {
      const xrCamera = this.renderer.xr.getCamera();
      xrCamera.getWorldPosition(this.head.position); xrCamera.getWorldQuaternion(this.head.quaternion);
    }
    this.sendPose(now, viewerEyes(viewer?.views));
    this.viewer.update();
    const decoded = this.viewer.displayedFrame;
    // A segment claiming packed stereo without a valid layout is unavailable, never shown as mono.
    const kind = decoded ? segmentKind(decoded.metadata) : null;
    const shown = kind === "invalid" ? null : decoded;
    this.lastFrameAt = frameSeenAt(this.lastFrameAt, this.lastFrameId, shown?.id ?? null, now);
    if (shown && shown.id !== this.lastFrameId) {
      this.lastFrameId = shown.id;
      this.stereoShown = kind === "stereo" && this.placeEyes(shown.pose);
      if (!this.stereoShown) this.place(shown.pose, shown.fovY);
    }
    this.presenting = macPresenting(true, this.lastFrameAt, now);
    this.picture.visible = this.presenting && !this.stereoShown;
    for (const quad of this.eyeQuads) quad.visible = this.presenting && this.stereoShown;
    this.paintLabel(!this.presenting ? LOST : this.stereoShown ? STEREO_LABEL : LABEL);
    // The label stays readable just under the gaze, independent of the picture's lag.
    this.label.position.copy(this.offset.set(0, -0.32, -1.5).applyQuaternion(this.head.quaternion).add(this.head.position));
    this.label.quaternion.copy(this.head.quaternion);
    this.group.visible = true;
    return this.presenting;
  }

  private start() {
    this.active = true;
    // Renderer view identity (not capture/map identity); a UUID where the page is a secure context.
    this.session = globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    this.lastFrameAt = -Infinity; this.lastFrameId = null; this.lastPoseAt = -Infinity; this.ring.clear();
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

  private sendPose(now: number, eyes: EyePose[]) {
    const socket = this.poseSocket;
    if (socket?.readyState !== WebSocket.OPEN || !shouldSendPose(this.lastPoseAt, now, socket.bufferedAmount)) return;
    const id = ++this.poseId;
    const position = this.head.position.toArray(), quaternion = this.head.quaternion.toArray();
    const message = mapPoseMessage({
      id, mono: now, wall: Date.now(), walk: mapWalk, head: { position, quaternion }, ray: null, eyes,
      session: this.session, mapRevision: this.mapRevision(), aspect: DEFAULT_ASPECT,
    });
    if (!message) return;
    this.lastPoseAt = now;
    this.ring.push({ id, mono: now, position, quaternion, eyes } satisfies WorldPose);
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

  /**
   * Place each eye's half where that eye was when the pair was requested, covering
   * exactly its (asymmetric) frustum. False when the pair's views are unknown.
   */
  private placeEyes(echo: Record<string, unknown> | null) {
    const from = this.ring.find(echo) as WorldPose | null;
    if (!from || from.eyes.length < 2) return false;
    return this.eyeQuads.every((quad, index) => {
      const eye = from.eyes[index], rect = frustumRect(eye.projection);
      if (!rect) return false;
      quad.quaternion.fromArray(eye.quaternion);
      quad.scale.set(rect.width, rect.height, 1);
      quad.position.set(rect.cx, rect.cy, -PICTURE_DISTANCE).applyQuaternion(quad.quaternion).add(this.offset.fromArray(eye.position));
      return true;
    });
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
