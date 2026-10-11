// Time-step encoding for sequence models (GRU, TCN, transformer). Mirrors paradigm_orion/sequence.py:
// the window is cut into fixed steps (100 ms by default), most recent at the end, each with 14 channels.
export const CHANNELS = ['moves', 'path', 'peak_speed', 'turning', 'pointer_downs', 'pointer_ups', 'click_hold', 'key_downs', 'key_ups',
  'key_hold', 'wheel', 'focus_changes', 'gap', 'active'];

/** events: normalized rows sorted by t, with t relative to the window start. Returns Float32Array(steps × 14). */
export function encodeSteps(events, durationMs, steps, stepMs) {
  const C = CHANNELS.length;
  const out = new Float32Array(steps * C);
  const path = new Float64Array(steps), peak = new Float64Array(steps), turn = new Float64Array(steps);
  const clickHold = new Float64Array(steps), keyHold = new Float64Array(steps);
  const offset = steps * stepMs - durationMs; // Shorter windows sit at the end; earlier steps stay empty.
  let pointers = new Map(), keys = new Map(), clicks = new Map();
  for (const row of events) {
    const i = Math.floor((row.t + offset) / stepMs);
    if (i < 0 || i >= steps) continue;
    const base = i * C;
    out[base + 13] = 1;
    switch (row.kind) {
      case 'boundary':
        out[base + 12] = 1; pointers = new Map(); keys = new Map(); clicks = new Map();
        break;
      case 'pointermove': {
        out[base]++;
        const id = row.pointer_id ?? 0, prev = pointers.get(id);
        if (prev && row.t > prev.t && row.t - prev.t < 100 && 'x' in row && 'y' in row) {
          const dx = row.x - prev.x, dy = row.y - prev.y, d = Math.hypot(dx, dy);
          path[i] += d;
          peak[i] = Math.max(peak[i], d * 1000 / (row.t - prev.t));
          if (d > 0.5 && prev.d > 0.5) {
            const cos = (dx * prev.dx + dy * prev.dy) / (d * prev.d);
            turn[i] += Math.acos(Math.max(-1, Math.min(1, cos)));
          }
          pointers.set(id, { t: row.t, x: row.x, y: row.y, dx, dy, d });
        } else if ('x' in row && 'y' in row) pointers.set(id, { t: row.t, x: row.x, y: row.y, dx: 0, dy: 0, d: 0 });
        break;
      }
      case 'pointerdown':
        out[base + 4]++;
        if (row.press_id !== undefined) clicks.set(row.press_id, row.t);
        break;
      case 'pointerup': {
        out[base + 5]++;
        const down = clicks.get(row.press_id);
        clicks.delete(row.press_id);
        if (down !== undefined && row.t >= down) clickHold[i] += row.t - down;
        break;
      }
      case 'keydown':
        if (!row.repeat) { out[base + 7]++; if (row.press_id !== undefined) keys.set(row.press_id, row.t); }
        break;
      case 'keyup': {
        out[base + 8]++;
        const down = keys.get(row.press_id);
        keys.delete(row.press_id);
        if (down !== undefined && row.t >= down) keyHold[i] += row.t - down;
        break;
      }
      case 'wheel': out[base + 10]++; break;
      case 'focusin': case 'focusout': out[base + 11]++; break;
    }
  }
  for (let i = 0; i < steps; i++) {
    const base = i * C;
    out[base + 1] = Math.log1p(path[i]); out[base + 2] = Math.log1p(peak[i]); out[base + 3] = Math.log1p(turn[i]);
    out[base + 6] = Math.log1p(clickHold[i]); out[base + 9] = Math.log1p(keyHold[i]);
  }
  return out;
}
