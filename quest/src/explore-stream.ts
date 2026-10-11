import * as THREE from "three";
import { H264Viewer } from "./h264";
import { mapWalk } from "./map-walk.js";
import { socketUrl } from "./mac-view-urls.js";
import { MAC_STALE_MS, PICTURE_DISTANCE, PoseRing, frameView, mapPoseMessage, pictureSize, shouldSendPose, viewerEyes } from "./mac-auto-pose.js";

type Eye = { eye?: string; position: number[]; quaternion: number[]; projection: number[] };
export type ExploreScene = { source?: string; geometry_source?: string; retained?: boolean; generation?: number;
  session_id?: string; map_epoch?: number; scene?: { floor_y: number; center: number[]; span: number } };

/** The headset draws only video quads. Dense fusion, meshing and stereo rendering stay on the Mac. */
export class ExploreStream {
  readonly viewer: H264Viewer;
  readonly group = new THREE.Group();
  readonly texture: THREE.VideoTexture;
  readonly eyes: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>[];
  private readonly flat: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>;
  private readonly ring = new PoseRing();
  private socket: WebSocket | null = null;
  private retry = 0;
  private session = crypto.randomUUID();
  private sequence = 0;
  private lastPose = -Infinity;
  private frameId: string | null = null;
  private seenAt = -Infinity;
  private stopped = true;
  private immersive = false;
  private stereoFrame = false;
  private readonly offset = new THREE.Vector3();
  private readonly position = new THREE.Vector3();
  private readonly orientation = new THREE.Quaternion();
  presenting = false;
  map: ExploreScene | null = null;
  frameAge: number | null = null;
  cameraState = "connecting";

  constructor(private renderer: THREE.WebGLRenderer, scene: THREE.Scene, private camera: THREE.PerspectiveCamera,
    private origin: string, readonly stereo = true) {
    this.viewer = new H264Viewer(() => {}, () => { this.seenAt = -Infinity; },
      socketUrl(origin, `/ws/macview${stereo ? "?layout=sbs-left-right" : ""}`));
    this.texture = new THREE.VideoTexture(this.viewer.video);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    const quad = () => {
      const mesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({
        map: this.texture, toneMapped: false, depthWrite: false }));
      mesh.renderOrder = -10; mesh.frustumCulled = false; return mesh;
    };
    this.flat = quad();
    this.eyes = [0, 1].map(half => {
      const mesh = quad(), uv = mesh.geometry.getAttribute("uv");
      for (let i = 0; i < uv.count; i++) uv.setX(i, half * .5 + uv.getX(i) * .5);
      mesh.layers.set(half + 1); return mesh;
    });
    this.group.add(this.flat, ...this.eyes); this.group.visible = false;
    scene.add(this.group);
  }
  start() { this.stopped = false; this.connectPose(); this.viewer.start(); }
  private connectPose() {
    if (this.stopped) return;
    const socket = new WebSocket(socketUrl(this.origin, "/ws/macview-pose"));
    this.socket = socket;
    socket.onopen = () => { this.cameraState = "connected"; };
    socket.onmessage = event => {
      const message = JSON.parse(String(event.data));
      if (message.type === "pose_rejected") this.cameraState = message.reason;
    };
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null; this.cameraState = "unavailable";
      if (!this.stopped) this.retry = window.setTimeout(() => this.connectPose(), 1000);
    };
  }
  /** A new pose ring on XR transitions prevents a desktop image being placed into the headset. */
  resetSession(immersive: boolean) {
    this.immersive = immersive; this.session = crypto.randomUUID(); this.ring.clear();
    this.seenAt = -Infinity; this.frameId = null; this.lastPose = -Infinity;
  }
  update(now: number, viewerPose?: XRViewerPose | null) {
    let eyes: Eye[];
    if (this.immersive) {
      if (!viewerPose) { this.group.visible = false; this.presenting = false; return false; }
      const { position: p, orientation: q } = viewerPose.transform;
      this.position.set(p.x, p.y, p.z); this.orientation.set(q.x, q.y, q.z, q.w);
      eyes = viewerEyes(viewerPose.views);
    } else {
      this.position.copy(this.camera.position); this.orientation.copy(this.camera.quaternion);
      eyes = this.stereo ? ["left", "right"].map((eye, i) => ({ eye,
        position: this.offset.set(i ? .032 : -.032, 0, 0).applyQuaternion(this.orientation).add(this.position).toArray(),
        quaternion: this.orientation.toArray(), projection: this.camera.projectionMatrix.toArray() })) : [];
    }
    const socket = this.socket;
    if (socket?.readyState === WebSocket.OPEN && shouldSendPose(this.lastPose, now, socket.bufferedAmount)) {
      const id = ++this.sequence, position = this.position.toArray(), quaternion = this.orientation.toArray();
      const walk = this.immersive ? mapWalk : { x: 0, y: 0, z: 0, yaw: 0 };
      const message = mapPoseMessage({ id, mono: now, wall: Date.now(), walk, head: { position, quaternion },
        eyes, session: this.session, aspect: this.camera.aspect });
      if (message) {
        this.ring.push({ id, mono: now, position, quaternion, eyes });
        this.lastPose = now; socket.send(JSON.stringify({ ...message, stereo: this.stereo && eyes.length === 2 }));
      }
    }
    this.viewer.update();
    const decoded = this.viewer.displayedFrame;
    if (decoded) {
      this.frameAge = decoded.age;
      if (decoded.age >= MAC_STALE_MS) this.seenAt = -Infinity;
      if (decoded.id !== this.frameId) {
        const view = decoded.age < MAC_STALE_MS ? frameView(decoded.metadata, decoded.pose, this.ring, this.session) : null;
        this.seenAt = view ? now - decoded.age : -Infinity;
        this.frameId = decoded.id; this.stereoFrame = view?.mode === "stereo";
        if (view) this.map = (decoded.metadata as unknown as { map?: ExploreScene }).map ?? null;
        if (view?.mode === "stereo") this.eyes.forEach((quad, i) => {
          const eye = view.eyes[i], rect = view.rects[i];
          quad.quaternion.fromArray(eye.quaternion); quad.scale.set(rect.width, rect.height, 1);
          quad.position.set(rect.cx, rect.cy, -PICTURE_DISTANCE).applyQuaternion(quad.quaternion).add(this.offset.fromArray(eye.position));
        });
        else if (view) {
          const from = view.from, video = this.viewer.video;
          const { width, height } = pictureSize(decoded.fovY ?? 50, video.videoWidth / video.videoHeight);
          this.flat.quaternion.fromArray(from?.quaternion ?? this.orientation.toArray());
          this.flat.scale.set(width, height, 1);
          this.flat.position.set(0, 0, -PICTURE_DISTANCE).applyQuaternion(this.flat.quaternion)
            .add(this.offset.fromArray(from?.position ?? this.position.toArray()));
        }
      }
    }
    this.presenting = now - this.seenAt < MAC_STALE_MS;
    this.group.visible = this.presenting;
    this.flat.visible = !this.stereoFrame;
    this.eyes.forEach((quad, i) => {
      quad.layers.set(i + 1);
      if (!this.immersive && i === 0) quad.layers.enable(0);
      quad.visible = this.stereoFrame && (this.immersive || i === 0);
    });
    return this.presenting;
  }
  dispose() {
    this.stopped = true; window.clearTimeout(this.retry);
    const socket = this.socket; this.socket = null; socket?.close(); this.viewer.stop();
    this.texture.dispose();
    for (const mesh of [this.flat, ...this.eyes]) { mesh.geometry.dispose(); mesh.material.dispose(); }
    this.group.removeFromParent();
  }
}
