// A shared bandwidth gate for complete map replacements. Mesh already carries
// positions/colors, so hybrid view derives its points locally instead of
// downloading a second full snapshot over cellular.
export const MAP_TRANSFER_INTERVAL_MS = 5000;
export function chooseMapTransfer({ needed, busy, now, lastStarted,
  pointsOnly, view, triangles, pointsTransport, pointRevision, meshRevision,
  appliedPoints, appliedMesh }) {
  if (!needed || busy || now - lastStarted < MAP_TRANSFER_INTERVAL_MS) return null;
  if (!pointsOnly && view === '3d' && triangles > 0) {
    return meshRevision === appliedMesh ? null : 'mesh';
  }
  return pointsTransport === 'http' && pointRevision !== appliedPoints ? 'points' : null;
}
