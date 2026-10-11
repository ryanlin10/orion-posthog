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
