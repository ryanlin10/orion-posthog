// Orion's built-in mood reading: simple heuristics on pointer speed, changes in direction and clicks, the same
// rules as the demo on paradigmmachines.ai/orion. Hand-written rules, not a trained classifier.
export const MOODS = ['angry', 'frustrated', 'calm', 'anxious', 'confused', 'pleased'];
export const MOOD_SPEC = {
  format: 'orion-model', format_version: 1, name: 'orion-mood', version: 1,
  target: { name: 'mood', type: 'categorical', classes: MOODS },
  window: { size_ms: 4000, align: 'past' }, estimator: { kind: 'rules' },
};
const MOOD_WIN = 2000;
const TAU = 2 * Math.PI;
const ramp = (v, a, b) => Math.max(0, Math.min(1, (v - a) / (b - a)));
const bell = (v, a, b, c) => Math.min(ramp(v, a, b), 1 - ramp(v, b, c));

/** Speed, changes in direction, and clicks over the last two seconds (clicks over four). */
export function moodFeatures(pts, presses, now) {
  const from = now - MOOD_WIN;
  const BIN = 50;
  const bins = new Uint8Array(MOOD_WIN / BIN);
  let path = 0;
  let jitter = 0;
  let shake = 0;
  let curl = 0;
  let chord = 0;
  let start = null;
  let prev = null;
  // A stroke ends at a reversal or a pause; its straightness is how far it got against how far it travelled.
  const endStroke = p => { if (start && p) chord += Math.hypot(p.x - start.x, p.y - start.y); start = p; };
  // Headings at two scales: 3 px steps catch jitter, 12 px steps catch side-to-side shakes and steady circling.
  // A reversal is a sharp turn across one step or two, as a turn can straddle a step.
  const fine = { step: 3, a: null, h: null, h2: null, run: 0 };
  const coarse = { step: 12, a: null, h: null, h2: null, run: 0 };
  const wrap = a => Math.abs(a - TAU * Math.round(a / TAU));
  const turnAt = (s, p) => {
    const d = Math.hypot(p.x - s.a.x, p.y - s.a.y);
    if (d < s.step) return;
    const hd = Math.atan2(p.y - s.a.y, p.x - s.a.x);
    if (s.h !== null) {
      const turn = hd - s.h - TAU * Math.round((hd - s.h) / TAU);
      const sharp = Math.max(wrap(turn), s.h2 === null ? 0 : wrap(hd - s.h2));
      if (s === fine) { if (sharp > 1.6) { if (s.run < 30) jitter++; s.run = 0; s.h = null; } }
      else if (sharp > 2) { if (s.run >= 40) shake++; s.run = 0; s.h = null; endStroke(s.a); }
      else curl += turn;
    }
    s.run += d;
    s.h2 = s.h;
    s.h = hd;
    s.a = p;
  };
  for (const p of pts) {
    if (p.t > now) break;
    if (p.t < from) { prev = p; continue; }
    bins[Math.min(bins.length - 1, Math.floor((p.t - from) / BIN))] = 1;
    if (p.brk || !prev || prev.t < from || p.t - prev.t > 100) {
      endStroke(prev && prev.t >= from ? prev : null);
      start = p;
      for (const s of [fine, coarse]) { s.a = p; s.h = s.h2 = null; s.run = 0; }
      prev = p;
      continue;
    }
    path += Math.hypot(p.x - prev.x, p.y - prev.y);
    prev = p;
    turnAt(fine, p);
    turnAt(coarse, p);
  }
  if (prev && prev.t >= from) endStroke(prev);
  let active = 0;
  let stops = 0;
  for (let i = 0; i < bins.length; i++) {
    active += bins[i];
    if (bins[i] && i + 2 < bins.length && !bins[i + 1] && !bins[i + 2]) stops++;
  }
  const secs = MOOD_WIN / 1000;
  const moving = active * BIN / 1000;
  const recent = presses.filter(c => c.down > now - 2 * MOOD_WIN && c.down <= now);
  let burst = 0;
  for (const c of recent) burst = Math.max(burst, recent.filter(o => o.down >= c.down && o.down - c.down < 1000).length);
  return {
    path,
    speed: active ? path / moving : 0,
    jitter: jitter / secs,
    shake: shake / secs,
    curl: active ? Math.abs(curl) / moving : 0,
    straight: path > 40 ? Math.min(1, chord / path) : 0.5,
    stops: stops / secs,
    cps: recent.length / (2 * secs),
    burst,
  };
}

export function moodScores(f) {
  const still = 1 - ramp(f.path, 20, 120);
  const turns = f.jitter + f.shake;
  return {
    angry: 0.5 * ramp(f.speed, 800, 1800) + 0.5 * ramp(f.burst, 2.5, 4.5) + 0.2 * ramp(f.shake, 1, 3.5),
    frustrated: 0.45 * ramp(f.shake, 0.6, 2) + 0.35 * ramp(f.cps, 0.5, 1.25) + 0.15 * bell(f.burst, 1.5, 3, 5) - 0.25 * ramp(f.speed, 1100, 1800),
    calm: (1 - ramp(f.speed, 150, 500)) * (1 - ramp(turns, 0.5, 2.5)) * (1 - 0.6 * ramp(f.cps, 0.3, 1)) * (1 - 0.7 * ramp(f.curl, 1.5, 3.5)) * (1 - still) + still,
    anxious: 0.75 * ramp(f.jitter, 0.75, 3.5) + 0.25 * ramp(f.stops, 0.4, 1.5) * (1 - still),
    confused: 0.5 * ramp(f.curl, 1, 3) + 0.25 * (1 - ramp(f.straight, 0.2, 0.6)) + 0.15 * ramp(f.stops, 0.3, 1.2) - 0.3 * ramp(f.speed, 800, 1500),
    pleased: (0.5 * ramp(f.straight, 0.6, 0.9) + 0.5 * bell(f.speed, 250, 600, 1200)) * (1 - ramp(turns, 0.4, 1.5)) * (1 - 0.5 * ramp(f.cps, 0.8, 1.5)) * (1 - still),
  };
}

export function moodProbs(s) {
  const e = MOODS.map(m => Math.exp(6 * s[m]));
  const z = e.reduce((a, b) => a + b, 0);
  return e.map(v => v / z);
}

/**
 * The demo's reader over Orion's input records: pointer samples and presses in, a smoothed mood out.
 * update(now) every ~100 ms eases toward each new reading, and a challenger must pull clearly ahead to take over.
 */
export function createMood() {
  let pts = [], presses = [], lastPt = -1e9, breakNext = true;
  let p = MOODS.map(m => (m === 'calm' ? 1 : 0)), lead = MOODS.indexOf('calm'), last = null, features = null;
  return {
    record(row) {
      if (row.kind === 'boundary') { breakNext = true; return; }
      if (row.kind === 'pointerdown') { presses.push({ down: row.t }); return; }
      if (row.kind !== 'pointermove' || typeof row.x !== 'number' || typeof row.y !== 'number') return;
      // A gap is a break in the path, as on the demo page.
      pts.push({ x: row.x, y: row.y, t: row.t, brk: breakNext || row.t - lastPt > 140 });
      breakNext = false;
      lastPt = row.t;
    },
    reset() {
      pts = []; presses = []; lastPt = -1e9; breakNext = true;
      p = MOODS.map(m => (m === 'calm' ? 1 : 0)); lead = MOODS.indexOf('calm'); last = null; features = null;
    },
    update(now) {
      pts = pts.filter(q => now - q.t <= 3000);
      presses = presses.filter(c => now - c.down <= 2 * MOOD_WIN);
      features = moodFeatures(pts, presses, now);
      const fresh = moodProbs(moodScores(features));
      const k = 1 - Math.exp(-Math.min(1000, last === null ? 1000 : now - last) / 400);
      last = now;
      fresh.forEach((v, i) => { p[i] += (v - p[i]) * k; });
      const best = p.indexOf(Math.max(...p));
      if (p[best] > p[lead] + 0.04) lead = best;
      return this.state();
    },
    state() {
      return { mood: MOODS[lead], confidence: p[lead], probabilities: Object.fromEntries(MOODS.map((m, i) => [m, p[i]])), features };
    },
  };
}

/** Plain-words readouts like the demo's: pointer speed, turns and clicks per second. */
export function moodEvidence(f) {
  if (!f) return [];
  return [`pointer speed ${Math.round(f.speed)} px/s`, `${(f.jitter + f.shake).toFixed(1)} turns/s`, `${f.cps.toFixed(1)} clicks/s`];
}
