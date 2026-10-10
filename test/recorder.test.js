import test from 'node:test';
import assert from 'node:assert/strict';
import { createRecorder } from '../src/index.js';
import { analyzeBatches } from '../src/analysis.js';
import { environment } from './helpers.js';

function setup(t, options = {}) {
  const env = environment();
  const recorder = createRecorder(env.posthog, { window: env.win, allowSynthetic: true, ...options });
  t.after(() => recorder.stop());
  return { ...env, recorder, rows: () => env.calls.flatMap(c => c[1].records) };
}
test('down, repeat and up preserve sub-ms timestamps without text values', t => {
  const e = setup(t);
  e.emit('keydown', { code: 'KeyA', key: 'a', timeStamp: 10.125, repeat: false, location: 0 });
  e.emit('keydown', { code: 'KeyA', timeStamp: 20.25, repeat: true, location: 0 });
  e.emit('keyup', { code: 'KeyA', timeStamp: 40.875, location: 0 });
  e.recorder.flush();
  assert.deepEqual(e.rows().map(r => r.kind), ['keydown', 'keydown', 'keyup']);
  assert.equal(e.rows()[0].t, 10.125);
  assert.equal('key' in e.rows()[0], false);
  assert.deepEqual(analyzeBatches(e.calls.map(c => c[1])).keyHoldMs, [30.75]);
});
test('all 120Hz coalesced samples retained; parent not duplicated', t => {
  const e = setup(t);
  const samples = Array.from({ length: 120 }, (_, i) => ({ timeStamp: i * 1000 / 120, clientX: i, clientY: 2, pointerId: 1 }));
  e.emit('pointermove', { timeStamp: 1000, getCoalescedEvents: () => samples });
  e.recorder.flush();
  assert.equal(e.rows().length, 120);
  assert.equal(e.rows()[119].x, 119);
  const result = analyzeBatches(e.calls.map(c => c[1]));
  assert.ok(Math.abs(result.movement.medianHz - 120) < 0.01);
});
test('selects raw updates without also capturing pointermove', t => {
  const e = environment(); e.win.onpointerrawupdate = null;
  const r = createRecorder(e.posthog, { window: e.win, allowSynthetic: true }); t.after(() => r.stop());
  e.emit('pointermove'); e.emit('pointerrawupdate'); r.flush();
  assert.equal(e.calls[0][1].records.length, 1);
  assert.equal(e.calls[0][1].capture.movement_source, 'pointerrawupdate');
});
test('bounded chunks are lossless at high movement rates', t => {
  const e = setup(t, { maxBytes: 4096, maxEvents: 9 });
  for (let i = 0; i < 1000; i++) e.emit('pointermove', { timeStamp: i, clientX: i });
  e.recorder.flush();
  assert.equal(e.rows().length, 1000);
  assert.equal(new Set(e.rows().map(r => r.t)).size, 1000);
  assert.ok(e.calls.every(c => Buffer.byteLength(JSON.stringify(c[1])) <= 4096));
  assert.equal(e.recorder.getStats().dropped, 0);
});
test('password and marked shadow-tree ancestors are excluded', t => {
  const e = setup(t), secret = { matches: () => true };
  e.doc.activeElement = secret; e.emit('keydown', { code: 'KeyA' });
  e.doc.activeElement = null;
  e.emit('pointermove', { composedPath: () => [e.target, secret] });
  e.recorder.flush();
  assert.equal(e.rows().length, 0);
  assert.equal(e.recorder.getStats().excluded, 2);
});
test('opt-out at flush drops buffered events', t => {
  const e = setup(t); e.emit('keydown'); e.optOut(true); e.recorder.flush();
  assert.equal(e.calls.length, 0); assert.equal(e.recorder.getStats().dropped, 1);
});
test('permission callback fails closed', t => {
  const e = setup(t, { isAllowed() { throw new Error('CMP unavailable'); } });
  e.emit('keydown'); e.recorder.flush(); assert.equal(e.calls.length, 0);
});
test('identity change never attributes old buffered input to a new person', t => {
  const e = setup(t); e.emit('keydown', { code: 'KeyA' });
  e.identity('session-b', 'person-b'); e.emit('keyup', { code: 'KeyB' }); e.recorder.flush();
  assert.equal(e.rows().some(r => r.code === 'KeyA'), false);
  assert.equal(e.calls[0][1].$session_id, 'session-b');
  assert.equal(e.rows()[0].reason, 'identity');
});
test('timing mode omits code and location; off mode omits keyboard', t => {
  const e = setup(t, { keyboard: 'timing' }); e.emit('keydown', { code: 'KeyA', location: 1 }); e.recorder.flush();
  assert.equal('code' in e.rows()[0], false); assert.equal('location' in e.rows()[0], false);
  e.recorder.stop(); const r = createRecorder(e.posthog, { window: e.win, keyboard: 'off', allowSynthetic: true });
  e.emit('keyup'); r.stop(); assert.equal(e.rows().length, 1);
});
test('idle gaps create distinct bursts; max chunk limits do not', t => {
  const e = setup(t, { maxEvents: 1 }); e.emit('keydown', { timeStamp: 10 }); e.emit('keyup', { timeStamp: 20 });
  e.emit('keydown', { timeStamp: 1000 }); e.recorder.flush();
  assert.equal(e.calls[0][1].burst_id, e.calls[1][1].burst_id);
  assert.notEqual(e.calls[1][1].burst_id, e.calls[2][1].burst_id);
});
test('blur marks a boundary without fabricating up events; pagehide uses beacon', t => {
  const e = setup(t); e.emit('keydown'); e.emit('blur', {}, e.win);
  assert.deepEqual(e.rows().map(r => r.kind), ['keydown', 'boundary']);
  e.emit('pagehide', {}, e.win);
  assert.deepEqual(e.calls.at(-1)[2], { transport: 'sendBeacon', send_instantly: true });
});
test('stop cleans listeners and timers; duplicate instances rejected', t => {
  const e = setup(t); assert.throws(() => createRecorder(e.posthog, { window: e.win }), /already/);
  e.recorder.stop(); e.emit('keydown'); assert.equal(e.rows().length, 0);
});
test('untrusted events excluded unless explicitly allowed', t => {
  const e = setup(t, { allowSynthetic: false }); e.emit('keydown'); e.recorder.flush(); assert.equal(e.calls.length, 0);
});
test('SDK errors are counted and cannot break the host page', t => {
  const e = setup(t); e.posthog.capture = () => { throw new Error('offline'); };
  e.emit('keydown'); assert.doesNotThrow(() => e.recorder.flush());
  assert.equal(e.recorder.getStats().errors, 1); assert.equal(e.recorder.getStats().dropped, 1);
});
test('idle timer sends a burst without another event', async t => {
  const e = setup(t, { idleMs: 50 }); e.emit('keydown');
  await new Promise(resolve => setTimeout(resolve, 80)); assert.equal(e.calls.length, 1);
});
