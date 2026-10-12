import { createRecorder, EVENT_NAME, SESSION_EVENT, SCHEMA_VERSION, MASKS, keyType, fieldType } from './capture.js';
import { createLive, LABEL_EVENT, MOOD_MODEL } from './live.js';
import { createSelfReport, SELF_REPORT_EVENT } from './survey.js';

export { createRecorder, createLive, createSelfReport, EVENT_NAME, SESSION_EVENT, LABEL_EVENT, SELF_REPORT_EVENT, MOOD_MODEL, SCHEMA_VERSION, MASKS, keyType, fieldType };
export { extractFeatures, createNormalizer } from './features.js';
export { predict, predictSteps, evidence, validateModel } from './model.js';
export { encodeSteps, channelsFor, CHANNELS } from './sequence.js';
export { MOODS, createMood, moodFeatures, moodScores, moodProbs } from './mood.js';
export { rankAnswers, POSITIVE, NEGATIVE } from './survey.js';
export const VERSION = '0.3.0';

async function fetchWith(win, url, read) {
  const response = await win.fetch(url, { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`Could not load Orion model ${url}: HTTP ${response.status}`);
  return read(response);
}

// One connection per PostHog instance: React runs effects twice in development and bundlers re-run modules on hot
// reload, so a second connect() returns the running connection instead of recording everything twice.
const connections = new WeakMap();

/**
 * Attach Orion to a loaded PostHog SDK: capture input, and label sessions live with any models given.
 * options.mask: 'timing' (default), 'types' or 'off'. options.models: model URLs or objects from `orion deploy`.
 * options.mood: true runs the built-in mood reading. options.selfReport: true (or settings) asks people how it is going.
 */
export function connect(posthog, options = {}) {
  const running = connections.get(posthog);
  if (running) return running;
  const win = options.window ?? globalThis.window;
  const live = options.live ?? {};
  const fetchJson = url => fetchWith(win, url, r => r.json());
  let labeller = null, report = null;
  const recorder = createRecorder(posthog, {
    ...options,
    onRecord: row => { labeller?.record(row); report?.record(row); },
    onReset: () => labeller?.reset(),
  });
  const selfReport = settings => createSelfReport({ win, posthog, permitted: recorder.permitted, recorderId: () => recorder.recorderId,
    mood: () => labeller.mood(), fetchJson, options: settings });
  try {
    labeller = createLive({ win, posthog, permitted: recorder.permitted, every: live.every ?? 2000,
      heartbeat: live.heartbeat ?? 60000, confirm: live.confirm ?? 2, send: live.send ?? true, fetchJson,
      fetchBytes: async url => new Uint8Array(await fetchWith(win, url, r => r.arrayBuffer())),
      ort: options.ort ?? win.ort });
    if (options.mood) labeller.load(MOOD_MODEL, options.mood === true ? {} : options.mood);
    if (options.selfReport) {
      // The question ranks its answers with the mood reading, which then runs without sending labels.
      if (!options.mood) labeller.load(MOOD_MODEL, { local: true });
      report = selfReport(options.selfReport === true ? {} : options.selfReport);
    }
  } catch (error) { report?.stop(); labeller?.stop(); recorder.stop(); throw error; }
  const models = options.models ?? [];
  const ready = Promise.allSettled(models.map(m => labeller.load(m))).then(results => {
    results.forEach((r, i) => { if (r.status === 'rejected') console.error('[orion]', typeof models[i] === 'string' ? models[i] : 'model', r.reason); });
    return results.filter(r => r.status === 'fulfilled').map(r => r.value);
  });
  if (recorder.permitted()) {
    const { mask, movementSource } = recorder.getStats();
    try {
      posthog.capture(SESSION_EVENT, { orion_version: VERSION, schema_version: SCHEMA_VERSION, mask,
        pointer_source: movementSource, models: models.map(m => (typeof m === 'string' ? m : m?.name)),
        mood: !!options.mood, self_report: !!options.selfReport });
    } catch { /* the SDK reports its own failures */ }
  }
  const connection = {
    version: VERSION,
    ready,
    load: source => labeller.load(source),
    context: labeller.context,
    contextText: labeller.contextText,
    labels: labeller.labels,
    onLabel: labeller.onLabel,
    mood: labeller.mood,
    /** Ask how it is going now (also without selfReport settings). Resolves with the answer, or null. */
    ask: () => (report ??= selfReport({ trigger: 'manual' })).ask('manual'),
    flush: recorder.flush,
    pause() { recorder.pause(); labeller.reset(); report?.hide(); },
    resume: recorder.resume,
    stop() { connections.delete(posthog); report?.stop(); labeller.stop(); recorder.stop(); },
    getStats: () => ({ ...recorder.getStats(), live: labeller.stats() }),
  };
  connections.set(posthog, connection);
  return connection;
}

export const orion = { connect, version: VERSION };
