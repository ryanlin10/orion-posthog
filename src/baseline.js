// Per-person baselines: how this browser usually behaves, from its earlier page loads. Mirrors
// paradigm_orion/dataset/baseline.py. Each page load is cut into tiles of the window length, counted from the page's
// time origin; a tile closes once input arrives grace_ms after it ends, and a closed tile that passes the window rules
// adds its features to running means kept in localStorage (averages of window features, never input). Windows on this
// page are scored against the means saved by earlier page loads: `<feature>_rel` is the value minus that mean.
import { extractFeatures } from './features.js';

export const INPUTLESS = new Set(['boundary', 'visible']);
export const AWAY = new Set(['hidden', 'blur', 'pagehide']); // The person left; nothing was lost.
const KEY = 'orion-baseline:';

/** Whether a gap with this reason rejects a window under a model's window rules. */
export const blocking = (reason, rules) => !rules.allow_gaps && !(rules.allow_away && AWAY.has(reason));

/** The rows of [a, b) sorted by time, or the reason the window cannot be scored. gaps: [start, end, reason]. */
export function windowRows(rows, gaps, a, b, rules) {
  const { min_events: minEvents = 2, max_events: maxEvents = 100000 } = rules;
  const inside = rows.filter(r => r.t >= a && r.t < b).sort((p, q) => p.t - q.t || p.ordinal - q.ordinal);
  if (inside.length > maxEvents) return { reason: 'too-many-events' };
  if (inside.filter(r => r.kind !== 'boundary').length < minEvents) return { reason: 'too-few-events' };
  if (gaps.some(([start, end, reason]) => start < b && end >= a && blocking(reason, rules))) return { reason: 'capture-gap' };
  return { events: inside };
}

/** Running means { feature: [mean, tiles] } after adding these tiles' features. */
export function mergeMeans(stored, tiles, cap) {
  const sums = new Map();
  for (const features of tiles) {
    for (const [name, value] of Object.entries(features)) {
      if (value === null) continue;
      const s = sums.get(name) ?? [0, 0];
      s[0] += value; s[1]++;
      sums.set(name, s);
    }
  }
  const out = { ...stored };
  for (const [name, [total, count]] of sums) {
    const [mean, n] = stored[name] ?? [0, 0];
    out[name] = [(mean * n + total) / (n + count), Math.min(n + count, cap)];
  }
  return out;
}

/** `<feature>_rel` for each feature: this window against the person's usual value, or null without enough history. */
export function relative(features, stored, minTiles) {
  const out = {};
  for (const [name, value] of Object.entries(features)) {
    const base = stored[name];
    out[`${name}_rel`] = value !== null && base && base[1] >= minTiles ? value - base[0] : null;
  }
  return out;
}

export function createBaseline(spec, storage) {
  const { tile_ms: size, grace_ms: grace, cap, min_tiles: minTiles } = spec.features.baseline;
  const families = spec.features.families;
  const key = KEY + spec.name;
  // A retrained model with another window or families starts afresh.
  const shape = JSON.stringify({ size, cap, families: [...families].sort() });
  let stored = {};
  try {
    const saved = JSON.parse(storage?.getItem(key) ?? 'null');
    if (saved?.shape === shape && saved.means && typeof saved.means === 'object') stored = saved.means;
  } catch { /* storage blocked or unreadable: start afresh */ }
  const tiles = [];
  let closed = -Infinity;
  return {
    /** Rows from here on belong to tiles still open, so live scoring must not prune them. */
    keepFrom: () => closed * size,
    /** An input row arrived at t: close the tiles that ended at least grace_ms before it. */
    advance(rows, gaps, t, rules) {
      const limit = Math.floor((t - grace) / size);
      if (limit <= closed) return;
      const open = [...new Set(rows.map(r => Math.floor(r.t / size)).filter(k => k >= closed && k < limit))].sort((a, b) => a - b);
      closed = limit;
      for (const k of open) {
        const w = windowRows(rows, gaps, k * size, (k + 1) * size, rules);
        if (w.events) tiles.push(extractFeatures(w.events, { start_ms: k * size, end_ms: (k + 1) * size }, families).features);
      }
      if (!open.length) return;
      try { storage?.setItem(key, JSON.stringify({ shape, means: mergeMeans(stored, tiles, cap) })); } catch { /* storage full or blocked */ }
    },
    relative: features => relative(features, stored, minTiles),
  };
}
