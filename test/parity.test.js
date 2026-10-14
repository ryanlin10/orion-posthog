// The browser computes the same window features as the Python dataset builder. Random input streams go
// through the real builder (python3, standard library only) and through features.js; every value must match.
import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createNormalizer, extractFeatures } from '../src/features.js';
import { encodeRecords } from '../src/codec.js';
import { createBaseline } from '../src/baseline.js';

function prng(seed) {
  return () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 2 ** 32; };
}

/** A messy stream: overlapping keys, repeats, lost releases, coalesced moves, clicks, wheel, focus and gaps. */
function randomStream(seed, ms = 30000) {
  const r = prng(seed), rows = [];
  let t = 1000 + r() * 50, press = 1, x = 400, y = 300;
  const held = [];
  while (t < ms) {
    const p = r();
    if (p < 0.25) {
      const id = press++;
      rows.push({ kind: 'keydown', t, repeat: false, press_id: id, trusted: true, target: 1 });
      if (r() < 0.1) rows.push({ kind: 'keydown', t: t + 30, repeat: true, press_id: id, trusted: true, target: 1 });
      if (r() > 0.05) held.push([id, t + 40 + r() * 140]);
    } else if (p < 0.6) {
      for (let k = 0; k < 1 + Math.floor(r() * 8); k++) {
        x += (r() - 0.5) * 30; y += (r() - 0.5) * 20;
        rows.push({ kind: 'pointermove', t: t + k * 8.33, x, y, pointer_id: 1, coalesced: true, trusted: true, target: 2 });
      }
    } else if (p < 0.7) {
      const button = r() < 0.9 ? 0 : 2;
      rows.push({ kind: 'pointerdown', t, x, y, pointer_id: 1, button, trusted: true, target: 3 });
      if (r() < 0.05) rows.push({ kind: 'pointercancel', t: t + 20, pointer_id: 1, trusted: true, target: 3 });
      else rows.push({ kind: 'pointerup', t: t + 60 + r() * 100, x, y, pointer_id: 1, button, trusted: true, target: 3 });
      rows.push({ kind: 'click', t: t + 61, x, y, pointer_id: 1, button, trusted: true, target: 3 });
    } else if (p < 0.8) {
      rows.push({ kind: 'wheel', t, dx: 0, dy: (r() - 0.5) * 200, dz: 0, mode: 0, trusted: true, target: 4 });
    } else if (p < 0.86) {
      rows.push({ kind: r() < 0.5 ? 'focusin' : 'focusout', t, field: 'text', trusted: true, target: 5 });
    } else if (p < 0.87) {
      rows.push({ kind: 'boundary', t, reason: 'blur' });
      held.length = 0;
    }
    for (let i = held.length - 1; i >= 0; i--) {
      if (held[i][1] <= t) { rows.push({ kind: 'keyup', t: held[i][1], repeat: false, press_id: held[i][0], trusted: true, target: 1 }); held.splice(i, 1); }
    }
    t += r() < 0.05 ? 1200 + r() * 2000 : 5 + r() * 120;
  }
  return rows;
}

function batches(rows, recorder, size = 300) {
  const out = [];
  for (let i = 0, seq = 0; i < rows.length; i += size, seq++) {
    const chunk = rows.slice(i, i + size);
    out.push({ event: 'orion_input', properties: { schema_version: '1.3', recorder_id: recorder, batch_sequence: seq, time_origin_ms: 1.7e12,
      capture: {}, ...(seq % 2 ? { record_columns: encodeRecords(chunk) } : { records: chunk }) } });
  }
  return out;
}

test('window features match the Python builder exactly', () => {
  const dir = mkdtempSync(join(tmpdir(), 'orion-parity-'));
  try {
    const families = ['keyboard', 'pointer', 'scroll', 'focus', 'volume'];
    const streams = [11, 12, 13, 14, 15, 16].map(seed => ({ id: `rec-${seed}`, rows: randomStream(seed) }));
    const labels = [], recordings = [];
    for (const s of streams) {
      recordings.push({ recorder_id: s.id, group_id: s.id });
      for (let a = 1000, i = 0; a < 29000; a += 2500, i++) {
        labels.push({ id: `${s.id}-${i}`, recorder_id: s.id, timebase: 'recorder_ms', start_ms: a, end_ms: a + 2500 * (0.4 + (i % 3) * 0.3),
          source: 'human', version: 'parity', confidence: 1, targets: { y: i % 2 ? 'a' : 'b' } });
      }
    }
    const jsonl = rows => rows.map(r => JSON.stringify(r)).join('\n') + '\n';
    writeFileSync(join(dir, 'batches.jsonl'), jsonl(streams.flatMap(s => batches(s.rows, s.id))));
    writeFileSync(join(dir, 'labels.jsonl'), jsonl(labels));
    writeFileSync(join(dir, 'recordings.jsonl'), jsonl(recordings));
    writeFileSync(join(dir, 'recipe.json'), JSON.stringify({ version: 1, targets: { y: { type: 'categorical', classes: ['a', 'b'] } },
      window: { mode: 'label' }, quality: { allow_gaps: true, min_events: 1 }, features: { families },
      split: { fractions: { train: 1, validation: 0, test: 0 } } }));
    execFileSync('python3', ['-m', 'paradigm_orion.dataset', '--recipe', join(dir, 'recipe.json'), '--batches', join(dir, 'batches.jsonl'),
      '--labels', join(dir, 'labels.jsonl'), '--recordings', join(dir, 'recordings.jsonl'), '--out', join(dir, 'out')], { stdio: 'pipe' });
    const examples = readFileSync(join(dir, 'out', 'train.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
    assert.ok(examples.length > 40, `only ${examples.length} examples were built`);
    let compared = 0;
    for (const s of streams) {
      const normalize = createNormalizer();
      const clean = s.rows.map((row, ordinal) => ({ ...normalize(row), ordinal }));
      for (const ex of examples.filter(e => e.recorder_id === s.id)) {
        const events = clean.filter(r => r.t >= ex.start_ms && r.t < ex.end_ms).sort((p, q) => p.t - q.t || p.ordinal - q.ordinal);
        const { features } = extractFeatures(events, { start_ms: ex.start_ms, end_ms: ex.end_ms }, families);
        assert.deepEqual(Object.keys(features).sort(), Object.keys(ex.features).sort());
        for (const [name, expected] of Object.entries(ex.features)) {
          const actual = features[name];
          if (expected === null) assert.equal(actual, null, `${ex.id} ${name}`);
          else assert.ok(Math.abs(actual - expected) <= 1e-9 * Math.max(1, Math.abs(expected)), `${ex.id} ${name}: js ${actual} vs python ${expected}`);
          compared++;
        }
      }
    }
    assert.ok(compared > 1000);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

/** Schema 1.4 input on top: field fills, returns to the page, scroll extents, aimed presses that overshoot, key classes. */
function richStream(seed, ms = 30000, { codes = false } = {}) {
  const r = prng(seed), rows = [];
  let t = 1000 + r() * 50, press = 1, x = 400, y = 300, scrollY = 0;
  const held = [];
  const KEYS = ['KeyA', 'Space', 'Backspace', 'Digit4', 'ShiftLeft', 'ArrowLeft', 'Delete', 'Period'];
  const TYPES = ['letter', 'letter', 'letter', 'delete', 'space', 'digit', 'modifier', 'navigation'];
  while (t < ms) {
    // Releases are delivered when they happen, before later input (the browser dispatches in time order).
    for (let i = held.length - 1; i >= 0; i--) {
      if (held[i][1] <= t) { rows.push({ kind: 'keyup', t: held[i][1], repeat: false, press_id: held[i][0], trusted: true, target: 1, ...held[i][2] }); held.splice(i, 1); }
    }
    const p = r();
    if (p < 0.2) {
      const id = press++;
      const key = codes ? { code: KEYS[Math.floor(r() * KEYS.length)], location: 0 } : r() < 0.85 ? { key_type: TYPES[Math.floor(r() * TYPES.length)] } : {};
      rows.push({ kind: 'keydown', t, repeat: false, press_id: id, trusted: true, target: 1, ...key });
      if (r() < 0.1) rows.push({ kind: 'keydown', t: t + 30, repeat: true, press_id: id, trusted: true, target: 1, ...key });
      if (r() > 0.05) held.push([id, t + 40 + r() * 140, key]);
    } else if (p < 0.4) {
      // A movement burst at 120 Hz, drifting one way with jitter, long enough to survive 40 ms thinning.
      const n = 1 + Math.floor(r() * 60), heading = r() * 2 * Math.PI;
      for (let k = 0; k < n; k++) {
        x += Math.cos(heading) * 4 + (r() - 0.5) * 6; y += Math.sin(heading) * 4 + (r() - 0.5) * 6;
        rows.push({ kind: 'pointermove', t: t + k * 8.33, x, y, pointer_id: 1, coalesced: true, trusted: true, target: 2 });
      }
      t += n * 8.33;
    } else if (p < 0.52) {
      // Aim at a control to the right, sometimes going past it and coming back, then press inside it.
      const box = { box_x: Math.round(x + 40), box_y: Math.round(y - 12), box_w: 80, box_h: 24 };
      const past = r() < 0.5 ? 2 + r() * 30 : 0;
      const path = [[x + 20, y], [box.box_x + 10, y], [box.box_x + box.box_w + past, y + (r() - 0.5) * 6], [box.box_x + 40, y]];
      for (const [px, py] of path) { t += 8 + r() * 20; rows.push({ kind: 'pointermove', t, x: px, y: py, pointer_id: 1, trusted: true, target: 2 }); }
      x = box.box_x + 40;
      t += 20 + r() * 80;
      const withBox = r() < 0.9 ? box : {};
      rows.push({ kind: 'pointerdown', t, x, y, pointer_id: 1, button: 0, trusted: true, target: 3, ...withBox });
      rows.push({ kind: 'pointerup', t: t + 60 + r() * 100, x, y, pointer_id: 1, button: 0, trusted: true, target: 3 });
    } else if (p < 0.58) {
      scrollY = Math.max(0, scrollY + (r() - 0.3) * 400);
      rows.push({ kind: 'scroll', t, x: 0, y: scrollY, trusted: true, target: 6, ...(r() < 0.8 ? { content_height: 6000, view_height: 900 } : {}) });
    } else if (p < 0.63) {
      rows.push({ kind: 'wheel', t, dx: 0, dy: (r() - 0.5) * 200, dz: 0, mode: 0, trusted: true, target: 4 });
    } else if (p < 0.68) {
      rows.push({ kind: 'input', t, field: 'text', via: ['paste', 'drop', 'fill'][Math.floor(r() * 3)], trusted: true, target: 5 });
    } else if (p < 0.7) {
      // Away from the tab: the boundary at the time it left, the return carried with the next input.
      const away = 300 + r() * 4000;
      rows.push({ kind: 'boundary', t, reason: 'hidden' });
      held.length = 0;
      t += away;
      rows.push({ kind: 'visible', t, hidden_ms: away });
    } else if (p < 0.75) {
      rows.push({ kind: r() < 0.5 ? 'focusin' : 'focusout', t, field: 'text', trusted: true, target: 5 });
    } else if (p < 0.76) {
      rows.push({ kind: 'boundary', t, reason: r() < 0.5 ? 'blur' : 'excluded' });
      held.length = 0;
    } else if (p < 0.8) {
      // A finger: contact size, and a pressure sensor on some devices (0.5 is what browsers report without one).
      const sensor = r() < 0.5;
      const touch = () => ({ pointer_id: 7, contact_w: 20 + r() * 30, contact_h: 20 + r() * 30, pressure: sensor ? r() : 0.5, trusted: true, target: 8 });
      rows.push({ kind: 'pointerdown', t, x, y, button: 0, ...touch() });
      for (let k = 1; k <= 6; k++) rows.push({ kind: 'pointermove', t: t + k * 16, x: x + k * 9, y: y - k * 4, ...touch() });
      rows.push({ kind: 'pointerup', t: t + 112, x: x + 54, y: y - 24, button: 0, pointer_id: 7, pressure: 0, trusted: true, target: 8 });
    }
    t += r() < 0.05 ? 1200 + r() * 2000 : 5 + r() * 120;
  }
  return rows;
}

const ALL_FAMILIES = ['keyboard', 'pointer', 'scroll', 'focus', 'volume', 'fills', 'visibility', 'depth', 'overshoot', 'corrections', 'typing',
  'motion', 'touch'];

function buildWithPython(dir, streams, labels, recipe) {
  const jsonl = rows => rows.map(r => JSON.stringify(r)).join('\n') + '\n';
  writeFileSync(join(dir, 'batches.jsonl'), jsonl(streams.flatMap(s => batches(s.rows, s.id).map(b => ({ ...b, properties: { ...b.properties,
    schema_version: '1.4', time_origin_ms: s.origin ?? 1.7e12 } })))));
  writeFileSync(join(dir, 'labels.jsonl'), jsonl(labels));
  writeFileSync(join(dir, 'recordings.jsonl'), jsonl(streams.map(s => ({ recorder_id: s.id, group_id: s.group ?? s.id, ...(s.browser ? { baseline_key: s.browser } : {}) }))));
  writeFileSync(join(dir, 'recipe.json'), JSON.stringify(recipe));
  execFileSync('python3', ['-m', 'paradigm_orion.dataset', '--recipe', join(dir, 'recipe.json'), '--batches', join(dir, 'batches.jsonl'),
    '--labels', join(dir, 'labels.jsonl'), '--recordings', join(dir, 'recordings.jsonl'), '--out', join(dir, 'out')], { stdio: 'pipe' });
  return readFileSync(join(dir, 'out', 'train.jsonl'), 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
}

function windowLabels(streams, step = 2500) {
  const labels = [];
  for (const s of streams) {
    for (let a = 1000, i = 0; a < 29000; a += step, i++) {
      labels.push({ id: `${s.id}-${i}`, recorder_id: s.id, timebase: 'recorder_ms', start_ms: a, end_ms: a + step * (0.4 + (i % 3) * 0.3),
        source: 'human', version: 'parity', confidence: 1, targets: { y: i % 2 ? 'a' : 'b' } });
    }
  }
  return labels;
}

function assertClose(actual, expected, what) {
  if (expected === null) assert.equal(actual, null, what);
  else assert.ok(Math.abs(actual - expected) <= 1e-9 * Math.max(1, Math.abs(expected)), `${what}: js ${actual} vs python ${expected}`);
}

test('schema 1.4 features (fills, time away, depth, overshoot, corrections, typing, motion, touch) match the Python builder exactly', () => {
  const dir = mkdtempSync(join(tmpdir(), 'orion-parity14-'));
  try {
    const streams = [21, 22, 23, 24, 25, 26].map((seed, i) => ({ id: `rec-${seed}`, rows: richStream(seed, 60000, { codes: i % 3 === 2 }) }));
    const examples = buildWithPython(dir, streams, [...windowLabels(streams), ...windowLabels(streams, 9000).map(l => ({ ...l, id: `long-${l.id}`,
      start_ms: l.start_ms + 30000, end_ms: l.end_ms + 30000 }))], { version: 1, targets: { y: { type: 'categorical', classes: ['a', 'b'] } },
      window: { mode: 'label' }, quality: { allow_gaps: true, min_events: 1 }, features: { families: ALL_FAMILIES },
      split: { fractions: { train: 1, validation: 0, test: 0 } } });
    assert.ok(examples.length > 40, `only ${examples.length} examples were built`);
    let compared = 0;
    const seen = { overshoot_px_median: 0, scroll_depth_max: 0, hidden_ms: 0, paste_count: 0, autofill_count: 0, correction_rate: 0,
      typing_flight_ms_median: 0, typing_pause_ms_sd: 0, motion_efficiency_median: 0, motion_crossings_mean: 0, motion_late_early_median: 0,
      motion_instability_mean: 0, motion_peak_speed_cv: 0, touch_area_cv: 0, touch_pressure_mean: 0 };
    for (const s of streams) {
      const normalize = createNormalizer();
      const clean = s.rows.map((row, ordinal) => ({ ...normalize(row), ordinal }));
      for (const ex of examples.filter(e => e.recorder_id === s.id)) {
        const events = clean.filter(r => r.t >= ex.start_ms && r.t < ex.end_ms).sort((p, q) => p.t - q.t || p.ordinal - q.ordinal);
        const { features } = extractFeatures(events, { start_ms: ex.start_ms, end_ms: ex.end_ms }, ALL_FAMILIES);
        assert.deepEqual(Object.keys(features).sort(), Object.keys(ex.features).sort());
        for (const [name, expected] of Object.entries(ex.features)) {
          assertClose(features[name], expected, `${ex.id} ${name}`);
          if (name in seen && expected) seen[name]++;
          compared++;
        }
      }
    }
    assert.ok(compared > 1000, `compared ${compared}`);
    // Every new feature took real values somewhere, so the comparison covered it.
    for (const [name, n] of Object.entries(seen)) assert.ok(n > 3, `${name} was zero or missing in all but ${n} windows`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("per-person baselines: the browser's stored means and _rel features match the Python builder", () => {
  const dir = mkdtempSync(join(tmpdir(), 'orion-baseline-'));
  try {
    const families = ['keyboard', 'pointer', 'scroll', 'visibility', 'overshoot'];
    const config = { tile_ms: 2500, grace_ms: 1000, cap: 6, min_tiles: 2 };
    const rules = { min_events: 2, max_events: 100000, allow_gaps: false, allow_away: true };
    // Two browsers, four page loads each, interleaved in time.
    const streams = [31, 32, 33, 34, 35, 36, 37, 38].map((seed, i) => ({ id: `rec-${seed}`, browser: `browser-${i % 2}`, group: `browser-${i % 2}`,
      origin: 1.7e12 + i * 60000, rows: richStream(seed) }));
    const examples = buildWithPython(dir, streams, windowLabels(streams), { version: 1, targets: { y: { type: 'categorical', classes: ['a', 'b'] } },
      window: { mode: 'label' }, quality: { allow_gaps: false, allow_away: true, min_events: 2 }, features: { families, baseline: config },
      split: { fractions: { train: 1, validation: 0, test: 0 } } });
    assert.ok(examples.length > 30, `only ${examples.length} examples were built`);
    const spec = { name: 'parity', features: { families, baseline: config } };
    const storages = new Map();
    let compared = 0, withHistory = 0;
    for (const s of [...streams].sort((p, q) => p.origin - q.origin)) {
      if (!storages.has(s.browser)) { const m = new Map(); storages.set(s.browser, { getItem: k => m.get(k) ?? null, setItem: (k, v) => m.set(k, v) }); }
      const base = createBaseline(spec, storages.get(s.browser));
      // Live order: each record is normalized, kept, and closes the tiles it can.
      const normalize = createNormalizer();
      const rows = [], gaps = [];
      let previousEnd = null;
      for (const [ordinal, row] of s.rows.entries()) {
        const clean = { ...normalize(row), ordinal };
        if (row.kind === 'boundary') gaps.push([Math.min(previousEnd ?? row.t, row.t), row.t, row.reason]);
        previousEnd = Math.max(previousEnd ?? row.t, row.t);
        rows.push(clean);
        if (row.kind !== 'boundary' && row.kind !== 'visible') base.advance(rows, gaps, row.t, rules);
      }
      for (const ex of examples.filter(e => e.recorder_id === s.id)) {
        const events = rows.filter(r => r.t >= ex.start_ms && r.t < ex.end_ms).sort((p, q) => p.t - q.t || p.ordinal - q.ordinal);
        const { features } = extractFeatures(events, { start_ms: ex.start_ms, end_ms: ex.end_ms }, families);
        const rel = base.relative(features);
        for (const [name, value] of Object.entries(rel)) {
          assertClose(value, ex.features[name], `${ex.id} ${name}`);
          if (value !== null) withHistory++;
          compared++;
        }
      }
    }
    assert.ok(compared > 500);
    assert.ok(withHistory > 100, `only ${withHistory} _rel values had history`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
