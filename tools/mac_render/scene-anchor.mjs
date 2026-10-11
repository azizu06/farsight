// A bounded sample on the Mac positions the viewer; no geometry goes to Quest.
export function sceneAnchor(chunks, fallback, count, sampleBudget = 4096) {
  const denseCount = chunks.reduce((sum, chunk) => sum + chunk.count, 0);
  const total = denseCount || count;
  if (!total) return null;
  const heights = [], low = [Infinity, Infinity, Infinity], high = [-Infinity, -Infinity, -Infinity];
  const step = Math.max(1, Math.ceil(total / sampleBudget));
  let base = 0, next = 0;
  const visit = (positions, used) => {
    for (; next < base + used; next += step) {
      const at = (next - base) * 3;
      heights.push(positions[at + 1]);
      for (let axis = 0; axis < 3; axis++) {
        low[axis] = Math.min(low[axis], positions[at + axis]);
        high[axis] = Math.max(high[axis], positions[at + axis]);
      }
    }
    base += used;
  };
  if (denseCount) for (const chunk of chunks) visit(chunk.positions, chunk.count);
  else visit(fallback, count);
  heights.sort((a, b) => a - b);
  return { floor_y: heights[Math.floor(heights.length * .03)],
    center: low.map((value, axis) => (value + high[axis]) / 2),
    span: Math.max(high[0] - low[0], high[1] - low[1], high[2] - low[2], 1) };
}
