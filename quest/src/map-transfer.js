// A shared bandwidth gate for complete map replacements. Mesh already carries
// positions/colors, so hybrid view derives its points locally instead of
// downloading a second full snapshot over cellular. A server-capped (stride
// decimated) mesh is too sparse to stand in for points, so then both are pulled,
// alternating, still one transfer at a time.
export const MAP_TRANSFER_INTERVAL_MS = 5000;
/** @param {{ needed: boolean, busy: boolean, now: number, lastStarted: number, pointsOnly: boolean,
 *   view: string, triangles: number, pointsTransport?: string, pointRevision: unknown,
 *   meshRevision: unknown, appliedPoints: unknown, appliedMesh: unknown, capped?: boolean,
 *   lastKind?: "mesh" | "points" | null }} state
 * @returns {"mesh" | "points" | null} */
export function chooseMapTransfer({ needed, busy, now, lastStarted,
  pointsOnly, view, triangles, pointsTransport, pointRevision, meshRevision,
  appliedPoints, appliedMesh, capped = false, lastKind = null }) {
  if (!needed || busy || now - lastStarted < MAP_TRANSFER_INTERVAL_MS) return null;
  const points = pointsTransport === 'http' && pointRevision !== appliedPoints ? 'points' : null;
  if (!pointsOnly && view === '3d' && triangles > 0) {
    const mesh = meshRevision === appliedMesh ? null : 'mesh';
    if (!capped) return mesh;
    return lastKind === 'mesh' ? points ?? mesh : mesh ?? points;
  }
  return points;
}
