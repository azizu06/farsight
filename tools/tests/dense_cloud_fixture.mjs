import assert from 'node:assert/strict';
import * as THREE from '../../quest/node_modules/three/build/three.module.js';
import { DenseCloudState, DenseCloudReceiver, DenseGpuChunks, decodeDenseDelta } from '../mac_render/dense-cloud.mjs';
const origin = process.argv[2];
const command = async path => { const result = await fetch(`${origin}/fixture/${path}`); assert.equal(result.status, 200); return result.json(); };
const state = new DenseCloudState({ capacity: 2_000_000, clock: () => 0 });
const receiver = new DenseCloudReceiver({ state, endpoint: `${origin}/dense/delta.bin`, clock: () => 0 });
const drain = async () => { let pages = 0; do { assert.equal(await receiver.pollOnce(), true, receiver.error); pages++; assert.ok(pages < 300); } while (receiver.more); };
try {
  await command('reset?capacity=20&journal=3&limit=2'); await command('step?count=6');
  assert.equal(await receiver.pollOnce(), true); assert.equal(state.size, 2); assert.equal(receiver.cursor.snapshot, 'true');
  const anchor = receiver.cursor.anchor;
  await command('delete?index=0'); await drain();
  assert.equal(receiver.cursor.after, anchor); await receiver.pollOnce();
  assert.equal(state.size, 5); assert.equal(state.cells.has('0_0_0'), false);
  await command('step?count=8'); await drain(); assert.equal(state.size, 13); assert.ok(receiver.resets >= 2, 'journal gap resync');
  await command('reset?capacity=2&journal=100&cache=1'); receiver.reset(); await receiver.pollOnce();
  await command('step?count=4'); await drain(); assert.equal(state.size, 4); assert.equal(receiver.sourcePoints, 2, 'working cache eviction is not deletion');
  await command('delete?index=3'); await drain(); assert.equal(state.size, 3); assert.equal(state.cells.has('3_0_0'), false);
  await command('reset?capacity=10&journal=100&session=second'); await receiver.pollOnce(); assert.equal(state.size, 0);
  const priorIdentity = receiver.identity;
  await command('step?count=2'); await drain(); await command('bad-source');
  assert.equal(await receiver.pollOnce(), false); assert.equal(state.size, 0); assert.ok(receiver.error.includes('identity'));
  assert.notEqual(receiver.identity, priorIdentity);

  // Actual Three.js attributes retain identity and update only changed slots.
  const small = new DenseCloudState({ capacity: 8, clock: () => 0 });
  const gpu = new DenseGpuChunks({ state: small, THREE, material: new THREE.PointsMaterial({ vertexColors: true }) });
  small.apply(new Int32Array([0,0,0,255,0,0,1, 10,0,0,0,255,0,1, 20,0,0,0,0,255,1])); gpu.flush();
  const slot = gpu.slots.get(0), attribute = slot.points.geometry.getAttribute('position'), array = attribute.array;
  assert.equal(gpu.metrics().gpu_uploaded_cells, 0); attribute.onUploadCallback(); assert.equal(gpu.metrics().gpu_uploaded_cells, 3);
  small.apply(new Int32Array([11,0,0,0,128,0,1])); gpu.flush();
  assert.equal(slot.points.geometry.getAttribute('position').array, array);
  assert.deepEqual(attribute.updateRanges, [{ start: 3, count: 3 }]); attribute.onUploadCallback();
  small.apply(new Int32Array([0,0,0,0,0,0,0])); gpu.flush(); assert.equal(slot.points.geometry.drawRange.count, 2);
  assert.equal(small.cells.has('0_0_0'), false); assert.deepEqual(attribute.updateRanges, [{ start: 0, count: 3 }]);
  attribute.onUploadCallback(); gpu.beginFrame(); slot.points.onAfterRender(); gpu.endFrame();
  assert.equal(gpu.metrics().gpu_drawn_cells, 2);
  small.clear(); gpu.flush(); assert.equal(gpu.slots.size, 0); gpu.dispose();

  // Real Python DenseMap HTTP pages accumulate beyond its 600k working cache.
  await command('reset?capacity=600000&journal=32768&cache=1'); receiver.reset(); await receiver.pollOnce();
  for (let index = 0; index < 2_000_000; index += 8192) {
    await command(`step?count=${Math.min(8192, 2_000_000 - index)}`); await drain();
  }
  assert.equal(state.size, 2_000_000); assert.equal(receiver.sourcePoints, 600000);
  assert.equal(state.cells.has('1999_999_0'), true);
  await command('delete?index=1500000'); await drain(); assert.equal(state.size, 1_999_999);
  assert.equal(state.cells.has('0_750_0'), false);
  await command('step?count=16'); await drain(); assert.equal(state.size, 2_000_000); assert.equal(state.localEvictions, 15);
  assert.equal(state.cells.has('0_0_0'), false, 'local cap removes real oldest cells');
  await command('reset?capacity=10&journal=100&session=second'); await receiver.pollOnce(); assert.equal(state.size, 0);
  const details = await command('metrics'); assert.equal(details.peak_dense_requests, 1);
  console.log(JSON.stringify({ passed: true, synthetic_fixture: true, frozen_ttl: true,
    accumulated_unique_cells: 2_000_000, source_working_cache_cells: 600000,
    max_records_per_page: details.max_records, dense_request_peak: details.peak_dense_requests,
    gpu_range_reuse_verified: true, explicit_tombstones_removed: true, anchored_resync_verified: true,
    physical_capture_qualified: false, gpu_capacity_render_qualified: false }));
} finally { receiver.dispose(); }
