const MAP_HEADERS = ['X-Map-Session', 'X-Map-Epoch', 'X-Map-Generation', 'X-Map-Revision'];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const session = value => typeof value === 'string' && UUID.test(value) ? value.toLowerCase() : null;
const integer = value => {
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]*)$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
};
const valid = value => Number.isSafeInteger(value) && value >= 0;

/** Reject a mismatched snapshot before reading its body. A newer revision of the
 * same origin is useful and must be recorded as the applied revision.
 * @param {Headers} headers
 * @param {{session_id: string|null, map_epoch: number|null, generation: number,
 *   revision: number, geometry_source?: string|null}} expected
 * @returns {number|null}
 */
export function mapResponseRevision(headers, expected) {
  if (!headers || !expected || !valid(expected.revision)) return null;
  const values = MAP_HEADERS.map(name => headers.get(name));
  const any = values.some(value => value !== null) ||
    [...headers.keys()].some(name => name.toLowerCase().startsWith('x-map-'));
  if (!any) return expected.geometry_source ? null : expected.revision;
  if (values.some(value => value === null)) return null;
  const actualSession = session(values[0]), wantedSession = session(expected.session_id);
  const epoch = integer(values[1]), generation = integer(values[2]), revision = integer(values[3]);
  if (!actualSession || !wantedSession || actualSession !== wantedSession ||
      !valid(expected.map_epoch) || !valid(expected.generation) ||
      epoch !== expected.map_epoch || generation !== expected.generation ||
      revision === null || revision < expected.revision) return null;
  return revision;
}
