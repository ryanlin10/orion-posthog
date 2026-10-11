// Window features computed in the browser. This mirrors paradigm_orion/dataset/features.py and the press
// pairing in pipeline.normalize(), so a model trained in Python sees the same numbers live.
// test/parity.test.js checks the two against each other on generated streams.

function median(values) {
  const s = Float64Array.from(values).sort();
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function summary(out, prefix, values) {
  if (!values.length) { out[`${prefix}_median`] = null; out[`${prefix}_cv`] = null; return; }
  let sum = 0;
  for (const v of values) sum += v;
  const mean = sum / values.length;
  let ss = 0;
  for (const v of values) ss += (v - mean) ** 2;
  out[`${prefix}_median`] = median(values);
  out[`${prefix}_cv`] = mean ? Math.sqrt(ss / values.length) / mean : null;
}

/** Assigns press IDs the way the Python builder does: in arrival order, cleared at boundaries. */
export function createNormalizer() {
  const held = new Map();
  let next = 0;
  return function normalize(row) {
    const clean = { kind: row.kind, t: row.t };
    for (const key of ['x', 'y', 'pointer_id', 'repeat']) if (row[key] !== undefined) clean[key] = row[key];
    if (row.kind === 'boundary') { held.clear(); return clean; }
    if (row.kind === 'keydown' || row.kind === 'keyup') {
      const hasPress = row.press_id !== undefined;
      const source = hasPress ? `p:${row.press_id}` : `c:${row.code}:${row.location ?? 0}`;
      const pairable = hasPress || !!row.code;
      if (row.kind === 'keydown' && !row.repeat && pairable) held.set(source, ++next);
      if (pairable && held.has(source)) clean.press_id = held.get(source);
      if (row.kind === 'keyup') held.delete(source);
      clean.repeat = row.repeat ?? false;
    }
    if (row.kind === 'pointerdown' || row.kind === 'pointerup' || row.kind === 'pointercancel') {
      const pointer = row.pointer_id ?? 0;
      const source = `ptr:${pointer}:${row.button ?? 0}`;
      if (row.kind === 'pointerdown') held.set(source, ++next);
      if (held.has(source)) clean.press_id = held.get(source);
      if (row.kind === 'pointerup') held.delete(source);
      if (row.kind === 'pointercancel') for (const key of [...held.keys()]) if (key.startsWith(`ptr:${pointer}:`)) held.delete(key);
    }
    return clean;
  };
}

/** events: normalized rows sorted by t (stable). Returns { features, quality }. */
export function extractFeatures(events, context, families) {
  const holds = [], intervals = [], movementDt = [], speeds = [], scrollDt = [], clickHolds = [], clickDt = [], dwell = [];
  const held = new Map(), pointers = new Map();
  let clickHeld = new Map();
  let previousDown = null, previousScroll = null, previousClick = null, focusStart = null;
  let downs = 0, unmatched = 0, overlaps = 0, path = 0;
  for (const row of events) {
    const { kind, t } = row;
    if (kind === 'boundary') {
      held.clear(); pointers.clear(); clickHeld.clear();
      previousDown = previousScroll = previousClick = focusStart = null;
      continue;
    }
    if (kind === 'keydown' && !row.repeat) {
      downs++;
      if (previousDown !== null && t >= previousDown) intervals.push(t - previousDown);
      previousDown = t;
      if (held.size) overlaps++;
      if (row.press_id !== undefined && row.press_id !== null) held.set(row.press_id, t);
    } else if (kind === 'keyup') {
      const start = held.get(row.press_id);
      held.delete(row.press_id);
      if (start === undefined || t < start) unmatched++;
      else holds.push(t - start);
    } else if (kind === 'pointermove') {
      const pointer = row.pointer_id ?? 0;
      const previous = pointers.get(pointer);
      if (previous && t > previous.t) {
        const dt = t - previous.t;
        // Long gaps are unknown paths; do not bridge idle periods.
        if (dt < 100 && 'x' in row && 'y' in row && 'x' in previous && 'y' in previous) {
          const distance = Math.hypot(row.x - previous.x, row.y - previous.y);
          movementDt.push(dt); speeds.push(distance * 1000 / dt); path += distance;
        }
      }
      pointers.set(pointer, row);
    } else if (kind === 'wheel') {
      if (previousScroll !== null && t >= previousScroll) scrollDt.push(t - previousScroll);
      previousScroll = t;
    } else if (kind === 'pointerdown') {
      if (previousClick !== null && t >= previousClick) clickDt.push(t - previousClick);
      previousClick = t;
      if (row.press_id !== undefined) clickHeld.set(row.press_id, [t, row.pointer_id ?? 0]);
    } else if (kind === 'pointerup') {
      const down = clickHeld.get(row.press_id);
      clickHeld.delete(row.press_id);
      if (down && t >= down[0]) clickHolds.push(t - down[0]);
    } else if (kind === 'pointercancel') {
      const pointer = row.pointer_id ?? 0;
      clickHeld = new Map([...clickHeld].filter(([, v]) => v[1] !== pointer));
    } else if (kind === 'focusin') {
      focusStart = t;
    } else if (kind === 'focusout') {
      if (focusStart !== null && t >= focusStart) dwell.push(t - focusStart);
      focusStart = null;
    }
  }
  const features = {};
  if (families.includes('keyboard')) {
    summary(features, 'key_hold_ms', holds);
    summary(features, 'key_interval_ms', intervals);
    features.key_overlap_fraction = downs ? overlaps / downs : null;
    features.key_pause_fraction = intervals.length ? intervals.filter(x => x >= 1000).length / intervals.length : null;
  }
  if (families.includes('pointer')) {
    summary(features, 'pointer_interval_ms', movementDt);
    summary(features, 'pointer_speed_px_s', speeds);
    summary(features, 'click_hold_ms', clickHolds);
    summary(features, 'click_interval_ms', clickDt);
  }
  if (families.includes('scroll')) summary(features, 'scroll_interval_ms', scrollDt);
  if (families.includes('focus')) summary(features, 'focus_dwell_ms', dwell);
  if (families.includes('volume')) {
    Object.assign(features, { key_down_count: downs, pointer_path_px: path, event_count: events.length, duration_ms: context.end_ms - context.start_ms });
  }
  const quality = { matched_key_pairs: holds.length, unmatched_key_ups: unmatched, open_key_downs: held.size,
    matched_pointer_pairs: clickHolds.length, movement_intervals: movementDt.length,
    measured_median_hz: movementDt.length ? 1000 / median(movementDt) : null };
  return { features, quality };
}
