// Live labelling in the visitor's browser. Every few seconds each loaded model scores the most recent
// window of input, using the same window rules as the training builder. Labels go to PostHog as
// `orion_label` events (for replays, search and webhooks) and to the page through context()/onLabel().
import { createNormalizer, extractFeatures } from './features.js';
import { predict, predictSteps, evidence, validateModel } from './model.js';
import { encodeSteps, CHANNELS } from './sequence.js';
import { MOOD_SPEC, createMood, moodEvidence } from './mood.js';
import { INPUTLESS, windowRows, createBaseline } from './baseline.js';
import { BASE_MODEL } from './base-model.js';

export const LABEL_EVENT = 'orion_label';
export const MOOD_MODEL = 'orion:mood';
export const BASE = 'orion:base';

export function createLive({ win, posthog, permitted, every = 2000, heartbeat = 60000, confirm = 2, history = 8, send = true, fetchJson, fetchBytes, ort }) {
  if (!Number.isInteger(every) || every < 250) throw new RangeError('Invalid live interval');
  const models = new Map();
  const listeners = new Set();
  const stats = { ticks: 0, scored: 0, noRead: 0, labelsSent: 0, errors: 0 };
  let normalize = createNormalizer();
  let rows = [], gaps = [], ordinal = 0, previousEnd = null, horizon = 0, timer, moodTimer, stopped = false, lastInput = -Infinity;

  function record(row) {
    const input = !INPUTLESS.has(row.kind);
    if (input) lastInput = Math.max(lastInput, row.t);
    for (const m of models.values()) m.mood?.record(row);
    const clean = normalize(row);
    clean.ordinal = ordinal++;
    if (row.kind === 'boundary') gaps.push([Math.min(previousEnd ?? row.t, row.t), row.t, row.reason]);
    previousEnd = Math.max(previousEnd ?? row.t, row.t);
    rows.push(clean);
    if (input) for (const m of models.values()) m.baseline?.advance(rows, gaps, row.t, m.spec.window);
  }
  function reset() {
    normalize = createNormalizer();
    rows = []; gaps = []; previousEnd = null;
    // Discards happen on consent loss, pause and session/person changes: labels must not carry over.
    for (const m of models.values()) {
      Object.assign(m, { label: undefined, since: 0, earlier: [], evidence: [], pending: null, streak: 0, lastSent: 0, sentLabel: undefined, sentAt: -Infinity });
      m.mood?.reset();
    }
  }
  function prune(now) {
    // Rows of baseline tiles that have not closed yet stay until they do.
    const cutoff = Math.min(now - horizon - 1000, ...[...models.values()].map(m => m.baseline?.keepFrom() ?? Infinity));
    if (rows.length && rows[0].t < cutoff) rows = rows.filter(r => r.t >= cutoff);
    if (gaps.length && gaps[0][1] < cutoff) gaps = gaps.filter(g => g[1] >= cutoff);
  }

  function windowFor(m, now) {
    const a = now - m.spec.window.size_ms, b = now;
    const w = windowRows(rows, gaps, a, b, m.spec.window);
    return w.events ? { events: w.events, context: { start_ms: a, end_ms: b } } : w;
  }

  function sessionId() { try { return posthog.get_session_id?.() ?? null; } catch { return null; } }
  function replayUrl() {
    try { return posthog.get_session_replay_url?.({ withTimestamp: true, timestampLookBack: 10 }) ?? null; } catch { return null; }
  }

  function snapshot(m) {
    if (m.label === undefined) return null;
    return { model: m.spec.name, version: m.spec.version, target: m.spec.target.name, label: m.label,
      confidence: m.confidence, probabilities: m.probabilities, since: new Date(m.since).toISOString(),
      earlier: [...m.earlier], evidence: [...m.evidence], agents: m.spec.agents ?? [],
      session_id: sessionId(), replay_url: replayUrl(), updated_at: new Date(m.updated).toISOString() };
  }

  function emit(m, changed) {
    const ctx = snapshot(m);
    m.lastSent = Date.now();
    m.sentLabel = m.label;
    if (send && !m.local) {
      try {
        const props = { model: ctx.model, model_version: ctx.version, target: ctx.target, label: ctx.label,
          confidence: ctx.confidence, probabilities: ctx.probabilities, evidence: ctx.evidence, since: ctx.since,
          earlier: ctx.earlier, agents: ctx.agents, window_ms: m.spec.window.size_ms, changed };
        if (posthog.capture(LABEL_EVENT, props) !== undefined || typeof posthog.__loaded !== 'boolean') stats.labelsSent++;
      } catch { stats.errors++; }
    }
    if (changed && !m.local) for (const fn of listeners) { try { fn(ctx); } catch { stats.errors++; } }
  }

  // The built-in mood reading updates every 100 ms like the demo. A new mood reaches PostHog and onLabel once it has
  // held for `hold` ms; the heartbeat repeats it only while there is input. Timing uses the page clock, as input does.
  function moodTick() {
    if (stopped || win.document?.hidden || !permitted()) return;
    const now = win.performance.now();
    for (const m of models.values()) {
      if (!m.mood) continue;
      const state = m.mood.update(now);
      if (state.mood !== m.label) {
        if (m.label !== undefined) { m.earlier.push(m.label); if (m.earlier.length > history) m.earlier.shift(); }
        m.label = state.mood; m.since = Date.now(); m.changedAt = now;
      }
      m.confidence = Math.round(state.confidence * 1000) / 1000;
      m.probabilities = Object.fromEntries(Object.entries(state.probabilities).map(([k, v]) => [k, Math.round(v * 1000) / 1000]));
      m.evidence = moodEvidence(state.features);
      m.updated = Date.now();
      const changed = m.label !== m.sentLabel;
      if (changed ? now - m.changedAt >= m.hold : now - m.sentAt >= heartbeat && lastInput > m.sentAt) {
        m.sentAt = now;
        emit(m, changed);
      }
    }
  }
  function loadMood({ local = false, hold = 2000, agents = [] } = {}) {
    const existing = models.get(MOOD_SPEC.name);
    if (existing) { existing.local = existing.local && local; return MOOD_SPEC.name; }
    models.set(MOOD_SPEC.name, { spec: { ...MOOD_SPEC, agents }, mood: createMood(), local, hold, label: undefined, since: 0, earlier: [],
      evidence: [], pending: null, streak: 0, lastSent: 0, sentLabel: undefined, changedAt: 0, sentAt: -Infinity, updated: 0, noRead: null });
    if (moodTimer === undefined && !stopped) moodTimer = win.setInterval(moodTick, 100);
    return MOOD_SPEC.name;
  }

  async function score(m, now) {
    const w = windowFor(m, now);
    if (!w.events) { stats.noRead++; m.noRead = w.reason; return; }
    const { features } = extractFeatures(w.events, w.context, m.spec.features.families);
    if (m.baseline) Object.assign(features, m.baseline.relative(features));
    let result;
    if (m.session) {
      const e = m.spec.estimator;
      const relative = w.events.map(r => ({ ...r, t: r.t - w.context.start_ms }));
      result = await predictSteps(m.spec, m.session, m.ort, encodeSteps(relative, m.spec.window.size_ms, e.steps, e.step_ms, e.channels ?? CHANNELS));
      if (stopped) return;
    } else result = predict(m.spec, features);
    stats.scored++;
    m.noRead = null; m.updated = Date.now();
    if (m.spec.target.type === 'continuous') {
      const changed = m.label === undefined;
      m.label = Math.round(result.value * 1000) / 1000; m.confidence = null; m.probabilities = null;
      m.evidence = evidence(m.spec, features);
      if (changed) m.since = Date.now();
      if (changed || Date.now() - m.lastSent >= heartbeat) emit(m, changed);
      return;
    }
    // A new label must win `confirm` ticks in a row before it replaces the current one.
    if (result.label === m.label) { m.pending = null; m.streak = 0; }
    else if (result.label === m.pending) m.streak++;
    else { m.pending = result.label; m.streak = 1; }
    const switching = m.label === undefined || (m.pending === result.label && m.streak >= confirm);
    if (switching) {
      if (m.label !== undefined) { m.earlier.push(m.label); if (m.earlier.length > history) m.earlier.shift(); }
      m.label = result.label; m.since = Date.now(); m.pending = null; m.streak = 0;
    }
    if (result.label === m.label) {
      m.confidence = Math.round(result.confidence * 1000) / 1000;
      m.probabilities = Object.fromEntries(Object.entries(result.probabilities).map(([k, v]) => [k, Math.round(v * 1000) / 1000]));
      m.evidence = evidence(m.spec, features);
    }
    if (switching || Date.now() - m.lastSent >= heartbeat) emit(m, switching);
  }

  /** Scores every model once; resolves when sequence models have answered. */
  function tick() {
    if (stopped) return Promise.resolve();
    stats.ticks++;
    const now = win.performance.now();
    prune(now);
    if (!permitted()) return Promise.resolve();
    return Promise.all([...models.values()].map(async m => {
      if (m.busy || m.mood) return; // A slow sequence model skips a tick rather than queueing work.
      m.busy = !!m.session; // Tabular models finish synchronously, inside this call.
      try { await score(m, now); } catch { stats.errors++; } finally { m.busy = false; }
    }));
  }

  async function sessionFor(spec, source, options) {
    if (options.session) return options.session;
    const runtime = options.ort ?? ort;
    if (!runtime?.InferenceSession) throw new Error(`${spec.name} is a sequence model: load onnxruntime-web (window.ort) before Orion`);
    const base = options.baseUrl ?? (typeof source === 'string' ? source : undefined);
    if (!base) throw new Error(`${spec.name}: pass baseUrl so the ONNX file can be found`);
    const bytes = await fetchBytes(new URL(spec.estimator.file, new URL(base, win.location?.href ?? 'http://localhost/')).href);
    return runtime.InferenceSession.create(bytes);
  }

  async function load(source, options = {}) {
    if (source === MOOD_MODEL) return loadMood(options);
    // The base model ships inside orion.js; a site's own models come from URLs or objects.
    const spec = validateModel(source === BASE ? { ...BASE_MODEL, agents: options.agents ?? [] } : typeof source === 'string' ? await fetchJson(source) : source);
    const session = spec.estimator.kind === 'onnx' ? await sessionFor(spec, source, options) : null;
    if (spec.window.align && spec.window.align !== 'past') {
      // Live input only has the past; a model trained on centred windows is scored on the most recent one.
      console.warn(`[orion] ${spec.name} was trained on ${spec.window.align} windows; live scoring uses the most recent ${spec.window.size_ms} ms`);
    }
    // Per-person baselines live in this browser's storage, written only while capture is permitted.
    let storage;
    try { storage = win.localStorage; } catch { /* blocked, e.g. in a sandboxed frame */ }
    const baseline = spec.features?.baseline && !session ? createBaseline(spec, storage) : null;
    models.set(spec.name, { spec, session, baseline, ort: options.ort ?? ort, busy: false, label: undefined, since: 0, earlier: [], evidence: [], pending: null, streak: 0, lastSent: 0, updated: 0, noRead: null });
    horizon = Math.max(...[...models.values()].filter(m => !m.mood).map(m => m.spec.window.size_ms));
    if (timer === undefined && !stopped) timer = win.setInterval(tick, every);
    return spec.name;
  }

  // Without a name: the site's own trained model with a label, then the base model, then the built-in mood reading.
  function pick(name) {
    if (name !== undefined) { const m = models.get(name); return m?.local ? undefined : m; }
    const ready = [...models.values()].filter(m => m.label !== undefined && !m.local);
    return ready.find(m => !m.mood && m.spec.name !== BASE_MODEL.name) ?? ready.find(m => !m.mood) ?? ready[0];
  }

  return {
    record, reset, load, tick,
    context(name) { const m = pick(name); return m ? snapshot(m) : null; },
    contextText(name) {
      const m = pick(name);
      if (!m) return '# Orion: no label yet\nuser_state: none';
      const c = snapshot(m);
      const lines = [`# from Orion · ${c.model} v${c.version}`, 'user_state:', `  ${c.target}: ${c.label}`];
      if (c.confidence !== null) lines.push(`  confidence: ${c.confidence.toFixed(2)}`);
      lines.push(`  since: ${c.since}`);
      if (c.earlier.length) lines.push(`  earlier: [${c.earlier.join(', ')}]`);
      if (c.evidence.length) lines.push(`  evidence: ${c.evidence.join('; ')}`);
      if (c.replay_url) lines.push(`  replay: ${c.replay_url}`);
      return lines.join('\n');
    },
    labels() { return [...models.values()].filter(m => !m.local).map(snapshot).filter(Boolean); },
    families: name => models.get(name)?.spec.features?.families,
    /** The built-in mood reading right now (also when it only runs locally for the self-report question). */
    mood() {
      const m = models.get(MOOD_SPEC.name);
      return m?.label === undefined ? null : { mood: m.label, confidence: m.confidence, probabilities: m.probabilities, since: new Date(m.since).toISOString() };
    },
    onLabel(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    stats() { return { ...stats, models: [...models.keys()].filter(k => !models.get(k).local), buffered: rows.length, noReadReasons: Object.fromEntries([...models].map(([k, m]) => [k, m.noRead])) }; },
    stop() { stopped = true; win.clearInterval(timer); win.clearInterval(moodTimer); timer = moodTimer = undefined; listeners.clear(); reset(); },
  };
}
