import test from 'node:test';
import assert from 'node:assert/strict';
import { createRecorder } from '../src/index.js';
import { analyzeBatches } from '../src/analysis.js';
import { webcrypto } from 'node:crypto';
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
  e.emit('keydown'); e.emit('pagehide', {}, e.win);
  assert.deepEqual(e.calls.at(-1)[2], { skip_client_rate_limiting: true, transport: 'sendBeacon', send_instantly: true });
});
test('lifecycle boundaries with nothing buffered ride on the next input instead of a separate event', t => {
  const e = setup(t); e.emit('keydown', { code: 'KeyA', timeStamp: 1 }); e.recorder.flush();
  e.emit('blur', {}, e.win); e.emit('pagehide', {}, e.win); e.recorder.pause(); e.recorder.resume();
  assert.equal(e.calls.length, 1);
  e.emit('keyup', { code: 'KeyA', timeStamp: 900 }); e.recorder.flush();
  assert.deepEqual(e.calls[1][1].records.map(r => [r.kind, r.t]), [['boundary', 900], ['keyup', 900]]);
  assert.deepEqual(analyzeBatches(e.calls.map(c => c[1])).keyHoldMs, []);
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

test('SDK identity lookup errors drop buffered input and still allow cleanup', t => {
  const e = setup(t); e.emit('keydown');
  e.posthog.get_session_id = () => { throw new Error('SDK unavailable'); };
  assert.doesNotThrow(() => e.recorder.stop());
  assert.equal(e.recorder.getStats().stopped, true);
  assert.equal(e.recorder.getStats().dropped, 1);
  assert.equal(e.recorder.getStats().errors, 1);
  const replacement = createRecorder(e.posthog, { window: e.win }); replacement.stop();
});

test('capture-only clients do not override SDK session enrichment with null', t => {
  const e = setup(t); delete e.posthog.get_session_id;
  e.emit('keydown'); e.recorder.flush();
  assert.equal('$session_id' in e.calls[0][1], false);
});

test('excluded input breaks key pairing without exposing the excluded events', t => {
  const e = setup(t);
  e.emit('keydown', { code: 'KeyA', timeStamp: 1 }); e.recorder.flush();
  e.doc.activeElement = { shadowRoot: { activeElement: { matches: () => true } } };
  e.emit('pointermove', { timeStamp: 2 });
  e.doc.activeElement = null;
  e.emit('keyup', { code: 'KeyA', timeStamp: 3 }); e.recorder.flush();
  assert.equal(e.rows().some(r => r.kind === 'pointermove'), false);
  assert.equal(e.rows().some(r => r.kind === 'boundary' && r.reason === 'excluded'), true);
  assert.deepEqual(analyzeBatches(e.calls.map(c => c[1])).keyHoldMs, []);
});

test('opt-out followed by opt-in cannot pair keys across the capture gap', t => {
  const e = setup(t);
  e.emit('keydown', { code: 'KeyA', timeStamp: 1 }); e.recorder.flush();
  e.optOut(true); e.emit('pointermove', { timeStamp: 2 });
  e.optOut(false); e.emit('keyup', { code: 'KeyA', timeStamp: 3 }); e.recorder.flush();
  assert.deepEqual(analyzeBatches(e.calls.map(c => c[1])).keyHoldMs, []);
});

test('SDK identity lookup recovers without retaining input from the failed lookup', t => {
  const e = setup(t), getter = e.posthog.get_session_id;
  e.emit('keydown', { code: 'KeyA', timeStamp: 1 }); e.recorder.flush();
  e.posthog.get_session_id = () => { throw new Error('temporary failure'); };
  e.emit('pointermove', { timeStamp: 2 });
  e.posthog.get_session_id = getter;
  e.emit('keyup', { code: 'KeyA', timeStamp: 3 }); e.recorder.flush();
  assert.equal(e.rows().some(r => r.kind === 'pointermove'), false);
  assert.equal(e.recorder.getStats().errors, 1);
  assert.deepEqual(analyzeBatches(e.calls.map(c => c[1])).keyHoldMs, []);
});

test('analyzer does not pair across a batch that the SDK failed to accept', t => {
  const e = setup(t), capture = e.posthog.capture;
  e.emit('keydown', { code: 'KeyA', timeStamp: 1 }); e.recorder.flush();
  e.posthog.capture = () => { throw new Error('temporary failure'); };
  e.emit('pointermove', { timeStamp: 2 }); e.recorder.flush();
  e.posthog.capture = capture;
  e.emit('keyup', { code: 'KeyA', timeStamp: 3 }); e.recorder.flush();
  const result = analyzeBatches(e.calls.map(c => c[1]));
  assert.equal(result.missingBatches, 1);
  assert.deepEqual(result.keyHoldMs, []);
});

test('the HTML snippet placeholder is rejected instead of silently queueing forever', () => {
  const e = environment();
  assert.throws(() => createRecorder({ ...e.posthog, _i: [] }, { window: e.win }), /snippet placeholder/);
});

test('batches the SDK refuses are counted as dropped and break continuity', t => {
  const e = environment(), capture = e.posthog.capture;
  e.posthog.__loaded = true; // posthog-js reports refusals by returning undefined
  e.recorder = createRecorder(e.posthog, { window: e.win, allowSynthetic: true }); t.after(() => e.recorder.stop());
  e.posthog.capture = (...args) => { e.calls.push(structuredClone(args)); return undefined; };
  e.emit('keydown', { code: 'KeyA', timeStamp: 1 }); e.recorder.flush();
  assert.deepEqual(e.recorder.getStats(), { ...e.recorder.getStats(), submitted: 0, batches: 0, dropped: 1 });
  e.posthog.capture = capture;
  e.emit('keyup', { code: 'KeyA', timeStamp: 3 }); e.recorder.flush();
  assert.equal(e.calls.at(-1)[1].records[0].reason, 'capture-gap');
  assert.ok(e.calls.every(c => c[2].skip_client_rate_limiting === true));
});

test('recorder IDs do not require a secure context', t => {
  const e = environment(); e.win.crypto = { getRandomValues: a => webcrypto.getRandomValues(a) };
  const r = createRecorder(e.posthog, { window: e.win, allowSynthetic: true }); t.after(() => r.stop());
  e.emit('keydown'); r.flush();
  assert.match(e.calls[0][1].recorder_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

test('batches carry a wall-clock anchor for replay alignment', t => {
  const e = setup(t); e.emit('keydown'); e.recorder.flush();
  const { clock } = e.calls[0][1];
  assert.ok(clock.perf_ms <= performance.now());
  assert.ok(Math.abs(clock.epoch_ms - Date.now()) < 1000);
});

test('identity and consent are read once per dispatched event, not per coalesced sample', t => {
  const e = setup(t); let lookups = 0;
  const getter = e.posthog.get_session_id; e.posthog.get_session_id = () => { lookups++; return getter(); };
  const samples = Array.from({ length: 8 }, (_, i) => ({ timeStamp: i, clientX: i, pointerId: 1 }));
  e.emit('pointermove', { timeStamp: 8, getCoalescedEvents: () => samples });
  assert.equal(e.rows().length + e.recorder.getStats().buffered, 8);
  assert.equal(lookups, 1);
});
