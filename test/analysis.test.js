import test from 'node:test';
import assert from 'node:assert/strict';
import { analyzeBatches } from '../src/analysis.js';

const batch = (sequence, records, recorder = 'r') => ({ recorder_id: recorder, batch_sequence: sequence, time_origin_ms: 1700000000000, records });
const key = (kind, t) => ({ kind, t, code: 'KeyA', location: 0 });
const move = t => ({ kind: 'pointermove', t, pointer_id: 1 });

test('analysis retains relative timestamp precision', () => {
  const result = analyzeBatches([batch(0, [key('keydown', 0.1), key('keyup', 0.3)])]);
  assert.equal(result.keyHoldMs[0], 0.3 - 0.1);
});
test('even interval counts use the mean of both middle values', () => {
  assert.equal(analyzeBatches([batch(0, [move(0), move(4), move(12)])]).movement.medianIntervalMs, 6);
});
test('duplicate and out-of-order batches do not duplicate input', () => {
  const first = batch(0, [key('keydown', 1)]), second = batch(1, [key('keyup', 5)]);
  const result = analyzeBatches([second, first, first]);
  assert.equal(result.records, 2);
  assert.deepEqual(result.keyHoldMs, [4]);
  assert.equal(result.duplicateBatches, 1);
});
test('missing batches break key and movement pairing', () => {
  const result = analyzeBatches([batch(0, [key('keydown', 1), move(2)]), batch(2, [key('keyup', 5), move(6)])]);
  assert.deepEqual(result.keyHoldMs, []);
  assert.equal(result.movement.measuredIntervals, 0);
  assert.equal(result.missingBatches, 1);
  assert.equal(result.unmatchedKeyUps, 1);
});
test('same key and pointer in separate recorders cannot pair', () => {
  const result = analyzeBatches([batch(0, [key('keydown', 1), move(2)], 'a'), batch(0, [key('keyup', 5), move(6)], 'b')]);
  assert.deepEqual(result.keyHoldMs, []);
  assert.equal(result.movement.measuredIntervals, 0);
});
