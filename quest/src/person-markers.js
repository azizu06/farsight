// 3D person markers: a labeled capsule at each tracked person's depth-backed map position
// (`world` on /ws/detections boxes, map frame, metres, Y up). Display only: markers live in
// their own group, never in the fused map, and disappear once a person goes unseen.
import * as THREE from "three";

/** A person unseen this long fades out; past PERSON_EXPIRE_MS the marker is removed. */
export const PERSON_FADE_MS = 1500, PERSON_EXPIRE_MS = 4000;

/** Tracked people with a finite 3D position from one detections packet.
 * @param {{ boxes?: any[], source?: string, simulated?: boolean } | null} packet */
export function peopleIn(packet) {
  const simulated = !!packet?.simulated || packet?.source === "SIMULATED";
  return (packet?.boxes ?? []).filter((box) => box?.cls === "person" && Number.isSafeInteger(box.id)
    && Array.isArray(box.world) && box.world.length === 3 && box.world.every(Number.isFinite))
    .map((box) => ({ id: box.id, world: box.world, simulated }));
}

/** Latest sighting per track id; opacity 1 while fresh, fading to 0, then dropped. */
export class PersonSightings {
  /** @type {Map<number, { world: number[], seenAt: number, simulated: boolean }>} */
  tracks = new Map();
  /** @param {{ id: number, world: number[], simulated: boolean }[]} people @param {number} seenAt */
  observe(people, seenAt) {
    for (const { id, world, simulated } of people) {
      if (!(seenAt < (this.tracks.get(id)?.seenAt ?? -Infinity))) this.tracks.set(id, { world, seenAt, simulated });
    }
  }
  /** @param {number} now */
  visible(now) {
    const shown = [];
    for (const [id, track] of this.tracks) {
      const age = now - track.seenAt;
      if (age >= PERSON_EXPIRE_MS) { this.tracks.delete(id); continue; }
      const opacity = age <= PERSON_FADE_MS ? 1 : 1 - (age - PERSON_FADE_MS) / (PERSON_EXPIRE_MS - PERSON_FADE_MS);
      shown.push({ id, ...track, opacity });
    }
    return shown;
  }
  clear() { this.tracks.clear(); }
}

const COLOR = 0xff5a36;

function labelTexture(text) {
  const canvas = document.createElement("canvas");
  canvas.width = 512; canvas.height = 128;
  const context = /** @type {CanvasRenderingContext2D} */ (canvas.getContext("2d"));
  context.fillStyle = "rgba(20, 8, 4, 0.82)";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.strokeStyle = "#ff5a36"; context.lineWidth = 8;
  context.strokeRect(4, 4, canvas.width - 8, canvas.height - 8);
  context.fillStyle = "#fff"; context.font = "bold 52px system-ui, sans-serif";
  context.textAlign = "center"; context.textBaseline = "middle";
  context.fillText(text, canvas.width / 2, canvas.height / 2);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

/** three.js view of PersonSightings: add `group` to the map root (map frame). */
export class PersonMarkers {
  group = new THREE.Group();
  sightings = new PersonSightings();
  /** @type {Map<number, THREE.Group>} */
  #markers = new Map();
  #body = new THREE.CapsuleGeometry(0.18, 0.9, 4, 12);

  constructor() { this.group.name = "person-markers"; }

  /** @param {any} packet newest detections packet, or null
   * @param {number} seenAt when that packet's frame arrived (performance.now clock) @param {number} now */
  update(packet, seenAt, now) {
    this.sightings.observe(peopleIn(packet), seenAt);
    const shown = this.sightings.visible(now), live = new Set();
    for (const person of shown) {
      live.add(person.id);
      const marker = this.#markers.get(person.id) ?? this.#create(person.id, person.simulated);
      // The torso point is ~0.3 m above the capsule centre for a standing adult.
      marker.position.set(person.world[0], person.world[1] - 0.3, person.world[2]);
      marker.traverse((part) => {
        const material = /** @type {any} */ (part).material;
        if (material) material.opacity = person.opacity * (material.userData.opacity ?? 1);
      });
    }
    for (const [id, marker] of this.#markers) if (!live.has(id)) this.#dispose(id, marker);
  }

  #create(id, simulated) {
    const marker = new THREE.Group();
    const body = new THREE.Mesh(this.#body, new THREE.MeshBasicMaterial({
      color: COLOR, transparent: true, depthWrite: false, toneMapped: false }));
    body.material.userData.opacity = 0.45;
    const label = new THREE.Sprite(new THREE.SpriteMaterial({
      map: labelTexture(`${simulated ? "SIM " : ""}PERSON #${id}`), transparent: true, depthTest: false, toneMapped: false }));
    label.scale.set(0.6, 0.15, 1);
    label.position.y = 0.95;
    label.renderOrder = 10;
    marker.add(body, label);
    this.group.add(marker);
    this.#markers.set(id, marker);
    return marker;
  }

  #dispose(id, marker) {
    this.group.remove(marker);
    marker.traverse((part) => {
      const material = /** @type {any} */ (part).material;
      if (!material) return;
      material.map?.dispose();
      material.dispose();
    });
    this.#markers.delete(id);
  }

  clear() {
    this.sightings.clear();
    for (const [id, marker] of this.#markers) this.#dispose(id, marker);
  }
}
