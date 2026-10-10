import test from "node:test";
import assert from "node:assert/strict";
import { VideoRequests } from "../src/video-requests.js";

test("pipelined replies retain their own RTT and never grow the window", () => {
  const requests = new VideoRequests(2);
  assert.deepEqual(requests.reserve(100), [1, 2]);
  assert.deepEqual(requests.reserve(110), []);
  assert.equal(requests.complete(1, 40, 210), 150);
  assert.deepEqual(requests.reserve(210), [3]);
  // Reply 2 arrived after requesting 3; it must retain time 100, not 210.
  assert.equal(requests.complete(2, 50, 300), 250);
  assert.deepEqual(requests.reserve(300), [4]);
  assert.equal(requests.complete(3, 0, 310), 100);
});

test("unsolicited, duplicate, stale-connection and malformed replies cannot gain freshness", () => {
  const requests = new VideoRequests();
  requests.reserve(100);
  assert.throws(() => requests.complete(4, 0, 110));
  assert.throws(() => requests.complete(1, NaN, 110));
  assert.throws(() => requests.complete(1, 0, 90));
  requests.complete(1, 0, 120);
  assert.throws(() => requests.complete(1, 0, 130));
  assert.equal(requests.expired(1101), true);
  requests.clear();
  assert.deepEqual(requests.reserve(1200), [3, 4]);
  assert.throws(() => requests.complete(2, 0, 1300));
});

test("only bounded measured server queue wait is subtracted; invalid metadata stays conservative", () => {
  for (const [wait, expected] of [[undefined, 230], [70, 160], [0, 230], [NaN, 230], [-1, 230], [201, 230], ["70", 230]]) {
    const requests = new VideoRequests();
    requests.reserve(100);
    assert.equal(requests.complete(1, 30, 300, wait), expected);
  }
});
