import assert from 'node:assert/strict';
import test from 'node:test';
import { mapResponseRevision } from '../src/map-response.js';

const SID = '12345678-1234-4321-9876-123456789abc';
const expected = (changes = {}) => ({ session_id: SID, map_epoch: 3, generation: 7,
  revision: 11, geometry_source: 'IPHONE ARKIT MESH', ...changes });
const headers = (changes = {}) => new Headers({ 'X-Map-Session': SID,
  'X-Map-Epoch': '3', 'X-Map-Generation': '7', 'X-Map-Revision': '11', ...changes });

test('same session accepts exact/newer snapshots and returns actual applied revision', () => {
  assert.equal(mapResponseRevision(headers(), expected()), 11);
  assert.equal(mapResponseRevision(headers({ 'X-Map-Revision': '12' }), expected()), 12);
  assert.equal(mapResponseRevision(headers({ 'X-Map-Revision': '10' }), expected()), null);
  assert.equal(mapResponseRevision(headers({ 'X-Map-Session': SID.toUpperCase() }), expected()), 11);
  assert.equal(mapResponseRevision(headers(), expected({ session_id: SID.toUpperCase() })), 11);
});

test('wrong session, epoch or generation cannot be applied', () => {
  for (const changed of [{ 'X-Map-Session': '22345678-1234-4321-9876-123456789abc' },
    { 'X-Map-Epoch': '4' }, { 'X-Map-Generation': '8' }]) {
    assert.equal(mapResponseRevision(headers(changed), expected()), null);
  }
});

test('every identity header is mandatory when any map header appears', () => {
  for (const key of ['X-Map-Session', 'X-Map-Epoch', 'X-Map-Generation', 'X-Map-Revision']) {
    const partial = headers(); partial.delete(key);
    assert.equal(mapResponseRevision(partial, expected()), null);
    assert.equal(mapResponseRevision(new Headers({ [key]: headers().get(key) }), expected()), null);
  }
  assert.equal(mapResponseRevision(new Headers({ 'X-Map-Future': '1' }), expected({ geometry_source: null })), null);
});

test('numeric headers require canonical nonnegative safe integers', () => {
  for (const key of ['X-Map-Epoch', 'X-Map-Generation', 'X-Map-Revision']) {
    for (const value of ['', '-1', '+3', '03', '3.0', '3e0', '0x3', 'NaN', 'Infinity', '9007199254740992']) {
      assert.equal(mapResponseRevision(headers({ [key]: value }), expected()), null, `${key}: ${value}`);
    }
  }
  assert.equal(mapResponseRevision(headers({ 'X-Map-Revision': '9007199254740991' }), expected()), Number.MAX_SAFE_INTEGER);
  assert.equal(mapResponseRevision(headers({ 'X-Map-Epoch': '0', 'X-Map-Generation': '0', 'X-Map-Revision': '0' }),
    expected({ map_epoch: 0, generation: 0, revision: 0 })), 0);
});

test('UUID headers and expected origin must be canonical UUIDs', () => {
  for (const value of ['', 'not-a-session', SID.replaceAll('-', ''), `{${SID}}`, `urn:uuid:${SID}`]) {
    assert.equal(mapResponseRevision(headers({ 'X-Map-Session': value }), expected()), null);
    assert.equal(mapResponseRevision(headers(), expected({ session_id: value })), null);
  }
  assert.equal(mapResponseRevision(headers(), expected({ session_id: null })), null);
});

test('legacy responses without identity headers remain compatible only outside geometry mode', () => {
  const legacy = new Headers({ 'Content-Type': 'application/octet-stream' });
  assert.equal(mapResponseRevision(legacy, expected()), null);
  assert.equal(mapResponseRevision(legacy, expected({ geometry_source: undefined, session_id: 'legacy-sim' })), 11);
  assert.equal(mapResponseRevision(legacy, expected({ geometry_source: null })), 11);
  // A partial identity header never falls back to legacy acceptance.
  assert.equal(mapResponseRevision(new Headers({ 'X-Map-Revision': '12' }), expected({ geometry_source: null })), null);
});

test('malformed expected counters cannot turn parsing errors into matches', () => {
  for (const key of ['map_epoch', 'generation', 'revision']) {
    for (const value of [null, undefined, -1, 3.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
      assert.equal(mapResponseRevision(headers(), expected({ [key]: value })), null);
    }
  }
  assert.equal(mapResponseRevision(new Headers(), expected({ geometry_source: null, revision: NaN })), null);
});
