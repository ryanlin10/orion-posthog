// Window features computed in the browser. This mirrors paradigm_orion/dataset/features.py and the press
// pairing in pipeline.normalize(), so a model trained in Python sees the same numbers live.
// test/parity.test.js checks the two against each other on generated streams.
import { keyType } from './capture.js';

// Fields that features read, kept by the normalizer as the Python builder keeps them.
const KEPT = ['x', 'y', 'pointer_id', 'repeat', 'hidden_ms', 'content_height', 'view_height', 'box_x', 'box_y', 'box_w', 'box_h', 'via',
  'pressure', 'contact_w', 'contact_h'];
// A press counts as overshooting when the pointer reached the control, left it by at least this much, and came back.
const OVERSHOOT_PX = 2;
const APPROACH_MS = 1000;
// Typing: a gap of a second or more between presses is a pause, not part of the rhythm.
const PAUSE_MS = 1000;
// Pointer movements: samples less than 100 ms apart belong to one movement; shorter or shorter-travelled ones are noise.
// Shapes read the pointer at most every 40 ms, so a 1000 Hz gaming mouse and a 60 Hz trackpad give comparable movements.
const THIN_MS = 40;
const MOVE_GAP_MS = 100;
const MIN_MOVE_SAMPLES = 5;
const MIN_MOVE_PX = 20;
const STEP_MS = 100;

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

const mean = values => { let sum = 0; for (const v of values) sum += v; return sum / values.length; };
function sd(values) {
  const m = mean(values);
  let ss = 0;
  for (const v of values) ss += (v - m) ** 2;
  return Math.sqrt(ss / values.length);
}
// Quartiles by linear interpolation (numpy's default), with the same arithmetic as features.py. Fewer than 4 values: null.
function quartiles(out, prefix, values) {
  const s = Float64Array.from(values).sort();
  const at = q => { const p = q * (s.length - 1), lo = Math.floor(p), hi = Math.ceil(p); return s[lo] + (s[hi] - s[lo]) * (p - lo); };
  const ok = s.length >= 4;
  out[`${prefix}_q1`] = ok ? at(0.25) : null;
  out[`${prefix}_median`] = ok ? at(0.5) : null;
  out[`${prefix}_q3`] = ok ? at(0.75) : null;
}

/** Typing rhythm: press-to-press and release-to-press intervals, hold times, and pauses. Timing only, no keys. */
function typingFeatures(out, events) {
  const downs = [], ups = new Map();
  let segment = 0;
  for (const row of events) {
    if (row.kind === 'boundary') segment++;
    else if (row.kind === 'keydown' && !row.repeat) downs.push({ t: row.t, id: row.press_id, segment });
    else if (row.kind === 'keyup' && row.press_id !== undefined && !ups.has(row.press_id)) ups.set(row.press_id, row.t);
  }
  const dd = [], flights = [], holds = [], pauses = [];
  downs.forEach((down, i) => {
    const up = down.id === undefined ? undefined : ups.get(down.id);
    if (up !== undefined && up >= down.t) holds.push(up - down.t);
    const next = downs[i + 1];
    if (!next || next.segment !== down.segment) return;
    const gap = next.t - down.t;
    if (gap >= PAUSE_MS) { pauses.push(gap); return; }
    dd.push(gap);
    if (up !== undefined && up >= down.t) flights.push(next.t - up);
  });
  quartiles(out, 'typing_dd_ms', dd);
  quartiles(out, 'typing_flight_ms', flights);
  quartiles(out, 'typing_hold_ms', holds);
  const paused = pauses.reduce((a, b) => a + b, 0), typed = dd.reduce((a, b) => a + b, 0);
  out.typing_pause_count = pauses.length;
  out.typing_pause_ms_mean = pauses.length ? mean(pauses) : null;
  out.typing_pause_ms_sd = pauses.length >= 2 ? sd(pauses) : null;
  out.typing_pause_time_fraction = paused + typed > 0 ? paused / (paused + typed) : null;
}

/** One pointer movement's shape: directness, deviation, reversals, turning, speed profile and short-scale instability. */
function movement(s, start) {
  const first = s[0], last = s[s.length - 1], dur = last.t - first.t;
  const cx = last.x - first.x, cy = last.y - first.y, chord = Math.hypot(cx, cy), quarter = dur / 4;
  let path = 0, peak = 0, peakT = first.t, turning = 0, steady = 0, sx = 0, sy = 0, early = 0, late = 0, prev = null;
  const bins = new Map();
  for (let k = 1; k < s.length; k++) {
    const a = s[k - 1], b = s[k], dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy);
    path += d;
    const v = d * 1000 / (b.t - a.t);
    if (v > peak) { peak = v; peakT = b.t; }
    // A reversal is a change in the sign of x or y movement, against the last direction that axis moved.
    let reversed = false;
    if (dx) { const g = dx > 0 ? 1 : -1; if (sx && g !== sx) reversed = true; sx = g; }
    if (dy) { const g = dy > 0 ? 1 : -1; if (sy && g !== sy) reversed = true; sy = g; }
    if (!reversed) steady++;
    if (d > 0.5) {
      if (prev) turning += Math.acos(Math.max(-1, Math.min(1, (dx * prev.dx + dy * prev.dy) / (d * prev.d))));
      prev = { dx, dy, d };
    }
    if (b.t <= first.t + quarter) early += d;
    if (a.t >= first.t + 3 * quarter) late += d;
    const bin = Math.floor((b.t - start) / STEP_MS);
    bins.set(bin, (bins.get(bin) ?? 0) + d);
  }
  if (path < MIN_MOVE_PX || dur <= 0) return null;
  let deviation = 0, crossings = 0, side = 0;
  for (const p of s) {
    const cross = (p.x - first.x) * cy - (p.y - first.y) * cx;
    deviation = Math.max(deviation, chord > 0 ? Math.abs(cross) / chord : Math.hypot(p.x - first.x, p.y - first.y));
    if (chord > 0 && cross) { const g = cross > 0 ? 1 : -1; if (side && g !== side) crossings++; side = g; }
  }
  const instability = [];
  for (const [bin, d] of bins) if (bins.has(bin + 1)) instability.push(Math.abs(bins.get(bin + 1) - d));
  return { efficiency: chord / path, excess: path - chord, deviation: deviation / Math.max(chord, MIN_MOVE_PX), crossings, consistency: steady / (s.length - 1),
    turning: turning * 100 / path, speed: path * 1000 / dur, peak, peakAt: (peakT - first.t) / dur, lateEarly: early > 0 ? late / early : null, instability };
}

/** Pointer movement shape over the window, from movements cut at pauses, presses, scrolls and gaps. */
function motionFeatures(out, events, context) {
  const moves = [], open = new Map();
  const close = pointer => {
    const s = open.get(pointer);
    open.delete(pointer);
    const m = s && s.length >= MIN_MOVE_SAMPLES ? movement(s, context.start_ms) : null;
    if (m) moves.push(m);
  };
  const closeAll = () => { for (const pointer of [...open.keys()]) close(pointer); };
  for (const row of events) {
    const { kind } = row;
    if (kind === 'boundary' || kind === 'wheel' || kind === 'scroll') closeAll();
    else if (kind === 'pointerdown' || kind === 'pointerup' || kind === 'pointercancel') close(row.pointer_id ?? 0);
    else if (kind === 'pointermove' && 'x' in row && 'y' in row) {
      const pointer = row.pointer_id ?? 0, s = open.get(pointer), last = s?.[s.length - 1];
      if (last && row.t - last.t < THIN_MS) continue; // Also skips repeated or earlier timestamps.
      if (last && row.t - last.t < MOVE_GAP_MS) s.push(row);
      else { close(pointer); open.set(pointer, [row]); }
    }
  }
  closeAll();
  const values = key => moves.map(m => m[key]).filter(v => v !== null);
  const middle = key => { const v = values(key); return v.length ? median(v) : null; };
  const peaks = values('peak'), diffs = moves.flatMap(m => m.instability);
  out.motion_moves = moves.length;
  out.motion_efficiency_median = middle('efficiency');
  out.motion_excess_px_median = middle('excess');
  out.motion_deviation_median = middle('deviation');
  out.motion_deviation_max = moves.length ? Math.max(...values('deviation')) : null;
  out.motion_crossings_mean = moves.length ? mean(values('crossings')) : null;
  out.motion_consistency_median = middle('consistency');
  out.motion_turning_median = middle('turning');
  out.motion_speed_median = middle('speed');
  out.motion_peak_speed_median = middle('peak');
  out.motion_peak_speed_cv = peaks.length >= 2 && mean(peaks) > 0 ? sd(peaks) / mean(peaks) : null;
  out.motion_peak_at_median = middle('peakAt');
  out.motion_late_early_median = middle('lateEarly');
  // Speed and directional consistency matter together (Banholzer et al. 2021), which a linear model needs spelled out.
  out.motion_speed_x_consistency = moves.length ? out.motion_speed_median * out.motion_consistency_median : null;
  out.motion_instability_mean = diffs.length ? mean(diffs) : null;
  out.motion_instability_peak = diffs.length ? Math.max(...diffs) : null;
}

/** Touch and pen contact: contact area and genuine pressure (0 and 0.5 are what browsers report without a sensor). */
function touchFeatures(out, events) {
  const areas = [], pressures = [];
  for (const row of events) {
    if ((row.kind !== 'pointerdown' && row.kind !== 'pointermove') || row.contact_w === undefined) continue;
    areas.push(row.contact_w * row.contact_h);
    if (row.pressure !== undefined && row.pressure !== 0 && row.pressure !== 0.5) pressures.push(row.pressure);
  }
  out.touch_area_mean = areas.length ? mean(areas) : null;
  out.touch_area_max = areas.length ? Math.max(...areas) : null;
  out.touch_area_cv = areas.length >= 2 && mean(areas) > 0 ? sd(areas) / mean(areas) : null;
  out.touch_pressure_mean = pressures.length ? mean(pressures) : null;
  out.touch_pressure_cv = pressures.length >= 2 && mean(pressures) > 0 ? sd(pressures) / mean(pressures) : null;
}

/** Assigns press IDs the way the Python builder does: in arrival order, cleared at boundaries. */
export function createNormalizer() {
  const held = new Map();
  let next = 0;
  return function normalize(row) {
    const clean = { kind: row.kind, t: row.t };
    for (const key of KEPT) if (row[key] !== undefined) clean[key] = row[key];
    if (row.kind === 'boundary') { held.clear(); return clean; }
    if (row.kind === 'keydown' || row.kind === 'keyup') {
      // The key class: sent with the types mask, or derived from the code with the code mask.
      if (row.key_type !== undefined) clean.key_type = row.key_type;
      else if (row.code) clean.key_type = keyType(row.code);
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

// Distance from a point to a box, zero inside it.
function outside(p, box) {
  const dx = Math.max(box.box_x - p.x, 0, p.x - (box.box_x + box.box_w));
  const dy = Math.max(box.box_y - p.y, 0, p.y - (box.box_y + box.box_h));
  return Math.hypot(dx, dy);
}

/** events: normalized rows sorted by t (stable). Returns { features, quality }. */
export function extractFeatures(events, context, families) {
  const holds = [], intervals = [], movementDt = [], speeds = [], scrollDt = [], clickHolds = [], clickDt = [], dwell = [], overshoots = [];
  const held = new Map(), pointers = new Map(), approach = new Map();
  let clickHeld = new Map();
  let previousDown = null, previousScroll = null, previousClick = null, focusStart = null, depth = null;
  let downs = 0, unmatched = 0, overlaps = 0, path = 0, classified = 0, deletes = 0, pastes = 0, autofills = 0, returns = 0, hidden = 0, aimed = 0;
  for (const row of events) {
    const { kind, t } = row;
    if (kind === 'boundary') {
      held.clear(); pointers.clear(); clickHeld.clear(); approach.clear();
      previousDown = previousScroll = previousClick = focusStart = null;
      continue;
    }
    if (kind === 'keydown' && !row.repeat) {
      downs++;
      if (row.key_type !== undefined) { classified++; if (row.key_type === 'delete') deletes++; }
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
      if ('x' in row && 'y' in row) {
        if (!approach.has(pointer)) approach.set(pointer, []);
        approach.get(pointer).push(row);
      }
    } else if (kind === 'wheel' || kind === 'scroll') {
      // The page moved under the pointer, so earlier positions no longer lead to the control.
      approach.clear();
      if (kind === 'wheel') {
        if (previousScroll !== null && t >= previousScroll) scrollDt.push(t - previousScroll);
        previousScroll = t;
      } else if (row.content_height > 0 && row.view_height !== undefined && row.y !== undefined) {
        depth = Math.max(depth ?? 0, Math.min(1, (row.y + row.view_height) / row.content_height));
      }
    } else if (kind === 'pointerdown') {
      if (previousClick !== null && t >= previousClick) clickDt.push(t - previousClick);
      previousClick = t;
      if (row.press_id !== undefined) clickHeld.set(row.press_id, [t, row.pointer_id ?? 0]);
      const pointer = row.pointer_id ?? 0;
      const trail = (approach.get(pointer) ?? []).filter(p => p.t >= t - APPROACH_MS);
      approach.delete(pointer);
      if (row.box_w !== undefined && trail.length) {
        aimed++;
        let reached = false, past = 0;
        for (const p of trail) {
          const d = outside(p, row);
          if (d === 0) reached = true;
          else if (reached) past = Math.max(past, d);
        }
        if (past >= OVERSHOOT_PX) overshoots.push(past);
      }
    } else if (kind === 'pointerup') {
      const down = clickHeld.get(row.press_id);
      clickHeld.delete(row.press_id);
      if (down && t >= down[0]) clickHolds.push(t - down[0]);
      approach.delete(row.pointer_id ?? 0);
    } else if (kind === 'pointercancel') {
      const pointer = row.pointer_id ?? 0;
      clickHeld = new Map([...clickHeld].filter(([, v]) => v[1] !== pointer));
      approach.delete(pointer);
    } else if (kind === 'input') {
      if (row.via === 'fill') autofills++;
      else pastes++;
    } else if (kind === 'visible') {
      // Only the part of the time away that falls inside this window.
      returns++;
      hidden += Math.min(row.hidden_ms, t - context.start_ms);
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
  if (families.includes('fills')) Object.assign(features, { paste_count: pastes, autofill_count: autofills });
  if (families.includes('visibility')) Object.assign(features, { hidden_count: returns, hidden_ms: hidden });
  if (families.includes('depth')) features.scroll_depth_max = depth;
  if (families.includes('overshoot')) {
    features.overshoot_fraction = aimed ? overshoots.length / aimed : null;
    features.overshoot_px_median = overshoots.length ? median(overshoots) : null;
    features.overshoot_px_sd = overshoots.length >= 2 ? sd(overshoots) : null;
  }
  if (families.includes('typing')) typingFeatures(features, events);
  if (families.includes('motion')) motionFeatures(features, events, context);
  if (families.includes('touch')) touchFeatures(features, events);
  // Needs the key class (mask 'types'); with the default timing mask it stays null.
  if (families.includes('corrections')) features.correction_rate = classified ? deletes / classified : null;
  const quality = { matched_key_pairs: holds.length, unmatched_key_ups: unmatched, open_key_downs: held.size,
    matched_pointer_pairs: clickHolds.length, movement_intervals: movementDt.length,
    measured_median_hz: movementDt.length ? 1000 / median(movementDt) : null };
  return { features, quality };
}
