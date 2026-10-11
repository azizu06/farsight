// A shared bandwidth gate for complete map replacements plus the additive
// /map/points.delta fast lane. Mesh already carries positions/colors, so
// hybrid view derives its points locally instead of downloading a second
// full snapshot over cellular. A server-capped (stride decimated) mesh is
// too sparse to stand in for points, so then both are pulled, alternating,
// still one transfer at a time. On a delta-capable server (status flag
// points_delta) the story changes: voxel-keyed cell updates merge in place
// at ~300 ms cadence while full pulls keep running on the authority cadence
// — cheap because the revision ETags answer unchanged polls with ~100 B
// 304s — and the mesh can no longer stand in for the keyed cloud, so mesh
// and points alternate like the capped case. Fallback servers keep the
// original five-second guards and revision-equality suppression exactly.
export const MAP_TRANSFER_INTERVAL_MS = 5000;
export const MAP_AUTHORITY_INTERVAL_MS = 2000;
export const DELTA_TRANSFER_INTERVAL_MS = 150;
/** @param {{ needed: boolean, busy: boolean, now: number, lastStarted: number, pointsOnly: boolean,
 *   view: string, triangles: number, pointsTransport?: string, pointRevision: unknown,
 *   meshRevision: unknown, appliedPoints: unknown, appliedMesh: unknown, capped?: boolean,
 *   lastKind?: "mesh" | "points" | null, pointsDelta?: boolean, lastDelta?: number }} state
 * @returns {"mesh" | "points" | "pointsDelta" | null} */
export function chooseMapTransfer({ needed, busy, now, lastStarted,
  pointsOnly, view, triangles, pointsTransport, pointRevision, meshRevision,
  appliedPoints, appliedMesh, capped = false, lastKind = null,
  pointsDelta = false, lastDelta = -Infinity }) {
  if (!needed || busy) return null;
  // Deltas cannot seed an empty cloud: appliedPoints < 0 forces the next
  // transfer to be a full pull. A missing appliedMesh in a mesh view fills
  // at the same time.
  const filling = pointsDelta &&
    (appliedPoints < 0 || (!pointsOnly && view === '3d' && triangles > 0 && appliedMesh < 0));
  const authority = pointsDelta ? MAP_AUTHORITY_INTERVAL_MS : MAP_TRANSFER_INTERVAL_MS;
  if (filling || now - lastStarted >= authority) {
    const points = pointsTransport === 'http' && (pointsDelta || pointRevision !== appliedPoints) ? 'points' : null;
    if (!pointsOnly && view === '3d' && triangles > 0) {
      const mesh = (pointsDelta || meshRevision !== appliedMesh) ? 'mesh' : null;
      if (!capped && !pointsDelta) return mesh;
      // Capped and delta-mode schedules both need points.bin as well as the
      // mesh — capped because the mesh is too sparse, delta because the keyed
      // cloud is authoritative — so they alternate full kinds on each tick.
      // Delta mode keeps revalidating already-current snapshots: the ETag
      // 304s cost ~100 B and they are what ages evictions out.
      const kind = lastKind === 'mesh' ? points ?? mesh : mesh ?? points;
      if (kind) return kind;
    } else if (points) return points;
  }
  if (pointsDelta && appliedPoints >= 0 && pointRevision > appliedPoints &&
      now - lastDelta >= DELTA_TRANSFER_INTERVAL_MS) return 'pointsDelta';
  return null;
}
