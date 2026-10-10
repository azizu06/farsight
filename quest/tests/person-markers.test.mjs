import assert from 'node:assert/strict';
import test from 'node:test';
import { PERSON_EXPIRE_MS, PERSON_FADE_MS, PersonMarkers, PersonSightings, peopleIn } from '../src/person-markers.js';

const box = (changes = {}) => ({ cls: 'person', id: 4, conf: 0.9, x: 0.4, y: 0.2, w: 0.2, h: 0.6, world: [1, 1.1, -2], ...changes });

test('only tracked people with a finite depth-backed position become markers', () => {
  const people = peopleIn({ source: 'IPHONE LIDAR', boxes: [
    box(), box({ id: 5, world: undefined }), box({ id: undefined }), box({ id: 6, cls: 'chair' }),
    box({ id: 7, world: [1, NaN, 2] }), box({ id: 8, world: [1, 2] }),
  ] });
  assert.deepEqual(people, [{ id: 4, world: [1, 1.1, -2], simulated: false }]);
  assert.deepEqual(peopleIn(null), []);
  assert.equal(peopleIn({ source: 'SIMULATED', boxes: [box()] })[0].simulated, true);
});

test('stale sightings fade and expire; an older packet never refreshes a track', () => {
  const sightings = new PersonSightings();
  sightings.observe([{ id: 4, world: [0, 1, -2], simulated: false }], 1000);
  assert.equal(sightings.visible(1000 + PERSON_FADE_MS)[0].opacity, 1);
  const fading = sightings.visible(1000 + (PERSON_FADE_MS + PERSON_EXPIRE_MS) / 2)[0].opacity;
  assert.ok(fading > 0 && fading < 1);
  sightings.observe([{ id: 4, world: [9, 9, 9], simulated: false }], 500);
  assert.deepEqual(sightings.visible(1100)[0].world, [0, 1, -2]);
  assert.deepEqual(sightings.visible(1000 + PERSON_EXPIRE_MS), []);
  assert.equal(sightings.tracks.size, 0);
});

test('the marker group follows sightings and is removed after expiry', () => {
  globalThis.document ??= { createElement: () => ({ getContext: () => new Proxy({}, { get: () => () => {} }) }) };
  const markers = new PersonMarkers();
  markers.update({ boxes: [box()] }, 0, 0);
  assert.equal(markers.group.children.length, 1);
  assert.deepEqual(markers.group.children[0].position.toArray().map((v) => +v.toFixed(3)), [1, 0.8, -2]);
  markers.update(null, 0, PERSON_EXPIRE_MS);
  assert.equal(markers.group.children.length, 0);
  markers.update({ boxes: [box()] }, 10, 10); markers.clear();
  assert.equal(markers.group.children.length, 0);
});
