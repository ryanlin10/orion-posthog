// The literature features on hand-worked input: typing rhythm, movement shape and touch contact.
import test from 'node:test';
import assert from 'node:assert/strict';
import { extractFeatures } from '../src/features.js';
import { createRecorder } from '../src/index.js';
import { decodeRecords } from '../src/codec.js';
import { environment } from './helpers.js';

const window = (events, families, end = 10000) => extractFeatures(events, { start_ms: 0, end_ms: end }, families).features;
const path = (points, t0 = 1000, dt = 40) => points.map(([x, y], i) => ({ kind: 'pointermove', t: t0 + i * dt, x, y, pointer_id: 1 }));

test('typing: press-to-press, release-to-press, holds and pauses', () => {
  const presses = [[0, 80], [100, 180], [250, 330], [1500, 1580], [1600, 1690]];
  const events = presses.flatMap(([down, up], i) => [{ kind: 'keydown', t: down, press_id: i + 1, repeat: false }, { kind: 'keyup', t: up, press_id: i + 1 }])
    .sort((a, b) => a.t - b.t);
  const f = window(events, ['typing']);
  assert.deepEqual([f.typing_dd_ms_q1, f.typing_dd_ms_median, f.typing_dd_ms_q3], [null, null, null], 'three rhythm intervals are too few');
  assert.equal(f.typing_pause_count, 1);
  assert.equal(f.typing_pause_ms_mean, 1250);
  assert.equal(f.typing_pause_time_fraction, 1250 / (1250 + 350));
  assert.deepEqual([f.typing_hold_ms_q1, f.typing_hold_ms_median, f.typing_hold_ms_q3], [80, 80, 80]);
  const more = [...events, { kind: 'keydown', t: 1700, press_id: 6, repeat: false }, { kind: 'keyup', t: 1760, press_id: 6 }];
  const g = window(more, ['typing']);
  // Rhythm intervals 100, 150, 100, 100; flights 20, 70, 20, 10 (the pause's flight is not rhythm).
  assert.deepEqual([g.typing_dd_ms_q1, g.typing_dd_ms_median, g.typing_dd_ms_q3], [100, 100, 112.5]);
  assert.deepEqual([g.typing_flight_ms_q1, g.typing_flight_ms_median, g.typing_flight_ms_q3], [17.5, 20, 32.5]);
});

test('motion: a straight movement is direct and steady; a wavering one is not', () => {
  const straight = window(path(Array.from({ length: 11 }, (_, i) => [100 + i * 10, 200])), ['motion']);
  assert.equal(straight.motion_moves, 1);
  assert.deepEqual([straight.motion_efficiency_median, straight.motion_deviation_max, straight.motion_crossings_mean,
    straight.motion_consistency_median, straight.motion_turning_median], [1, 0, 0, 1, 0]);
  assert.equal(straight.motion_speed_median, 250);
  const wavering = window(path(Array.from({ length: 11 }, (_, i) => [100 + i * 10, 200 + (i % 2 ? 15 : -15) * (i > 0 && i < 10)])), ['motion']);
  assert.ok(wavering.motion_efficiency_median < 0.6);
  assert.ok(wavering.motion_crossings_mean >= 7);
  assert.ok(wavering.motion_consistency_median < 0.3);
  assert.ok(wavering.motion_deviation_max > 0.1);
  assert.ok(wavering.motion_turning_median > straight.motion_turning_median);
});

test('motion: movements split at pauses and presses; tiny or short ones are ignored', () => {
  const a = path(Array.from({ length: 6 }, (_, i) => [i * 10, 0]), 0);
  const b = path(Array.from({ length: 4 }, (_, i) => [100 + i * 10, 0]), 400); // after a pause; only 4 samples before the press
  const press = { kind: 'pointerdown', t: 530, pointer_id: 1, button: 0 };
  const c = path(Array.from({ length: 6 }, (_, i) => [200 + i * 10, 0]), 531);
  const tiny = path(Array.from({ length: 6 }, (_, i) => [400 + i, 0]), 2000);
  const f = window([...a, ...b, press, ...c, ...tiny], ['motion']);
  assert.equal(f.motion_moves, 2, 'a and c count; b has too few samples before the press, the tiny one travels 5 px');
});

test('motion: short-scale instability compares the travel of neighbouring 100 ms steps', () => {
  // 50 px in the first 100 ms step (steps ending at 41 and 82 ms), then 4 px in each later step: |4 - 50|, then 0s.
  const points = [[0, 0], [25, 0], [50, 0], [52, 0], [54, 0], [56, 0], [58, 0]];
  const events = points.map(([x, y], i) => ({ kind: 'pointermove', t: i < 3 ? i * 41 : 82 + (i - 2) * 50, x, y, pointer_id: 1 }));
  const f = window(events, ['motion']);
  assert.equal(f.motion_instability_peak, 46);
});

test('motion: the same movement sampled at 125 Hz and at 25 Hz gives the same shape', () => {
  const at = hz => Array.from({ length: Math.round(hz * 0.8) + 1 }, (_, i) => {
    const t = i * 1000 / hz;
    return { kind: 'pointermove', t: 1000 + t, x: 100 + 0.5 * t, y: 200 + 30 * Math.sin(t / 120), pointer_id: 1 };
  });
  const fast = window(at(125), ['motion']), slow = window(at(25), ['motion']);
  for (const name of ['motion_efficiency_median', 'motion_deviation_max', 'motion_consistency_median', 'motion_speed_median']) {
    assert.ok(Math.abs(fast[name] - slow[name]) <= 0.05 * Math.abs(slow[name]) + 1e-9, `${name}: ${fast[name]} vs ${slow[name]}`);
  }
});

test('touch: contact area and only genuine pressure', () => {
  const events = [
    { kind: 'pointerdown', t: 0, contact_w: 10, contact_h: 20, pressure: 0.5, pointer_id: 2 },
    { kind: 'pointermove', t: 16, contact_w: 20, contact_h: 20, pressure: 0.31, pointer_id: 2 },
    { kind: 'pointermove', t: 32, x: 1, y: 1, pressure: 0.9, pointer_id: 1 }, // a mouse: no contact size, so not touch
  ];
  const f = window(events, ['touch']);
  assert.deepEqual([f.touch_area_mean, f.touch_area_max, f.touch_pressure_mean, f.touch_pressure_cv], [300, 400, 0.31, null]);
  assert.equal(window([], ['touch']).touch_area_mean, null);
});

test('the recorder keeps contact size for touch and pen, not for a mouse', t => {
  const e = environment();
  const r = createRecorder(e.posthog, { window: e.win, allowSynthetic: true });
  t.after(() => r.stop());
  e.emit('pointerdown', { pointerType: 'touch', width: 23.5, height: 30, pressure: 0.4, clientX: 5, clientY: 5, timeStamp: 1 });
  e.emit('pointerdown', { pointerType: 'mouse', width: 1, height: 1, clientX: 5, clientY: 5, timeStamp: 2 });
  r.flush();
  const [touch, mouse] = decodeRecords(e.calls.find(c => c[0] === 'orion_input')[1]);
  assert.deepEqual([touch.contact_w, touch.contact_h], [23.5, 30]);
  assert.ok(!('contact_w' in mouse));
});
