import * as THREE from "three";
import { navActive, navColor, parseNav, ribbon } from "./nav-view.js";

export type Nav = NonNullable<ReturnType<typeof parseNav>>;

// Just above the display grid (y = -0.002) so the glow never z-fights it.
const FLOOR_Y = 0.015;
const PATH_HALF_WIDTH = 0.09;
const FRONTIER_COLOR = "#c792ff";

/** Glowing planned path, goal beacon, frontier markers and aim reticle, in voxel-map world coordinates (map x, z on the y = 0 floor). */
export class NavOverlay {
  readonly group = new THREE.Group();
  private key = "";
  private pathMaterial = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide, toneMapped: false,
    uniforms: { color: { value: new THREE.Color() }, time: { value: 0 }, flow: { value: 1 } },
    vertexShader: `attribute float side; attribute float along; varying float vSide; varying float vAlong;
      void main() { vSide = side; vAlong = along; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
    fragmentShader: `uniform vec3 color; uniform float time; uniform float flow; varying float vSide; varying float vAlong;
      void main() {
        float edge = 1.0 - abs(vSide);
        float glow = smoothstep(0.0, 1.0, edge);
        float core = smoothstep(0.55, 1.0, edge);
        // Chevrons flowing toward the goal while the planner is live.
        float pulse = flow * smoothstep(0.75, 1.0, fract(vAlong * 2.0 - time * 1.2)) * glow;
        vec3 rgb = mix(color, vec3(1.0), core * 0.7 + pulse * 0.5);
        gl_FragColor = vec4(rgb, glow * 0.45 + core * 0.55 + pulse * 0.4);
      }`,
  });
  readonly path = new THREE.Mesh(new THREE.BufferGeometry(), this.pathMaterial);
  readonly goal = new THREE.Group();
  private goalRing = new THREE.Mesh(new THREE.RingGeometry(0.16, 0.24, 48), new THREE.MeshBasicMaterial({
    color: "#ffd166", transparent: true, opacity: 0.95, side: THREE.DoubleSide, depthWrite: false, toneMapped: false }));
  private beamMaterial = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide, toneMapped: false,
    uniforms: { color: { value: new THREE.Color("#ffd166") } },
    vertexShader: "varying float vY; void main() { vY = uv.y; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
    fragmentShader: "uniform vec3 color; varying float vY; void main() { gl_FragColor = vec4(color, 0.55 * (1.0 - vY)); }",
  });
  private goalBeam = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.12, 1.4, 24, 1, true), this.beamMaterial);
  readonly frontiers = new THREE.InstancedMesh(new THREE.CircleGeometry(0.1, 4), new THREE.MeshBasicMaterial({
    color: FRONTIER_COLOR, transparent: true, opacity: 0.85, side: THREE.DoubleSide, depthWrite: false, toneMapped: false }), 50);
  readonly reticle = new THREE.Mesh(new THREE.RingGeometry(0.12, 0.17, 32), new THREE.MeshBasicMaterial({
    color: "#ffffff", transparent: true, opacity: 0.9, side: THREE.DoubleSide, depthWrite: false, depthTest: false, toneMapped: false }));
  private matrix = new THREE.Matrix4();

  constructor() {
    this.group.name = "nav-overlay";
    this.path.renderOrder = 5; this.path.frustumCulled = false; this.path.visible = false;
    this.goalRing.rotation.x = -Math.PI / 2;
    this.goalBeam.position.y = 0.7;
    this.goal.add(this.goalRing, this.goalBeam); this.goal.visible = false; this.goal.renderOrder = 6;
    this.frontiers.count = 0; this.frontiers.frustumCulled = false; this.frontiers.renderOrder = 5;
    this.reticle.rotation.x = -Math.PI / 2; this.reticle.visible = false; this.reticle.renderOrder = 7;
    this.group.add(this.path, this.goal, this.frontiers, this.reticle);
  }

  /** Rebuilds geometry only when the planner's output changes; animates every call. */
  update(nav: Nav | null, now: number) {
    const key = nav ? JSON.stringify([nav.state, nav.goal, nav.path, nav.frontiers]) : "";
    if (key !== this.key) { this.key = key; this.rebuild(nav); }
    const t = now / 1000;
    this.pathMaterial.uniforms.time.value = t;
    const pulse = 1 + 0.12 * Math.sin(t * 4);
    this.goalRing.scale.setScalar(this.goal.visible && nav && navActive(nav) ? pulse : 1);
  }

  /** Aim point on the floor (map x, z) while the operator is choosing a goal; null hides it. */
  setReticle(point: { x: number; z: number } | null) {
    this.reticle.visible = !!point;
    if (point) this.reticle.position.set(point.x, FLOOR_Y + 0.005, point.z);
  }

  private rebuild(nav: Nav | null) {
    const geometry = nav ? ribbon(nav.path, PATH_HALF_WIDTH, FLOOR_Y) : null;
    this.path.geometry.dispose();
    this.path.geometry = new THREE.BufferGeometry();
    if (geometry) {
      this.path.geometry.setAttribute("position", new THREE.BufferAttribute(geometry.positions, 3));
      this.path.geometry.setAttribute("side", new THREE.BufferAttribute(geometry.side, 1));
      this.path.geometry.setAttribute("along", new THREE.BufferAttribute(geometry.along, 1));
      this.path.geometry.setIndex(new THREE.BufferAttribute(geometry.indices, 1));
    }
    this.path.userData.points = geometry?.points ?? 0;
    this.path.visible = !!geometry;
    if (nav) {
      this.pathMaterial.uniforms.color.value.set(navColor(nav.state));
      this.pathMaterial.uniforms.flow.value = navActive(nav) ? 1 : 0;
    }
    this.goal.visible = !!nav?.goal;
    if (nav?.goal) {
      this.goal.position.set(nav.goal.x, FLOOR_Y, nav.goal.z);
      const color = nav.state === "failed" ? navColor("failed") : nav.state === "arrived" ? navColor("arrived") : "#ffd166";
      (this.goalRing.material as THREE.MeshBasicMaterial).color.set(color);
      this.beamMaterial.uniforms.color.value.set(color);
    }
    const frontiers: number[][] = nav?.frontiers ?? [];
    frontiers.forEach(([x, z], i) => this.frontiers.setMatrixAt(i, this.matrix.makeRotationX(-Math.PI / 2).setPosition(x, FLOOR_Y, z)));
    this.frontiers.count = frontiers.length;
    this.frontiers.instanceMatrix.needsUpdate = true;
  }

  dispose() {
    this.path.geometry.dispose(); this.pathMaterial.dispose();
    this.goalRing.geometry.dispose(); (this.goalRing.material as THREE.Material).dispose();
    this.goalBeam.geometry.dispose(); this.beamMaterial.dispose();
    this.frontiers.geometry.dispose(); (this.frontiers.material as THREE.Material).dispose(); this.frontiers.dispose();
    this.reticle.geometry.dispose(); (this.reticle.material as THREE.Material).dispose();
  }
}
