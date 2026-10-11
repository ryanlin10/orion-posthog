import { createRecorder, EVENT_NAME, SESSION_EVENT, SCHEMA_VERSION, MASKS, keyType, fieldType } from './capture.js';
import { createLive, LABEL_EVENT } from './live.js';

export { createRecorder, createLive, EVENT_NAME, SESSION_EVENT, LABEL_EVENT, SCHEMA_VERSION, MASKS, keyType, fieldType };
export { extractFeatures, createNormalizer } from './features.js';
export { predict, predictSteps, evidence, validateModel } from './model.js';
export { encodeSteps, CHANNELS } from './sequence.js';
export const VERSION = '0.1.0';

async function fetchWith(win, url, read) {
  const response = await win.fetch(url, { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`Could not load Orion model ${url}: HTTP ${response.status}`);
  return read(response);
}

/**
 * Attach Orion to a loaded PostHog SDK: capture input, and label sessions live with any models given.
 * options.mask: 'timing' (default), 'types' or 'off'. options.models: model URLs or objects from `orion deploy`.
 */
export function connect(posthog, options = {}) {
  const win = options.window ?? globalThis.window;
  const live = options.live ?? {};
  let labeller = null;
  const recorder = createRecorder(posthog, {
    ...options,
    onRecord: row => labeller?.record(row),
    onReset: () => labeller?.reset(),
  });
  try {
    labeller = createLive({ win, posthog, permitted: recorder.permitted, every: live.every ?? 2000,
      heartbeat: live.heartbeat ?? 60000, confirm: live.confirm ?? 2, send: live.send ?? true,
      fetchJson: url => fetchWith(win, url, r => r.json()),
      fetchBytes: async url => new Uint8Array(await fetchWith(win, url, r => r.arrayBuffer())),
      ort: options.ort ?? win.ort });
  } catch (error) { recorder.stop(); throw error; }
  const models = options.models ?? [];
  const ready = Promise.allSettled(models.map(m => labeller.load(m))).then(results => {
    results.forEach((r, i) => { if (r.status === 'rejected') console.error('[orion]', typeof models[i] === 'string' ? models[i] : 'model', r.reason); });
    return results.filter(r => r.status === 'fulfilled').map(r => r.value);
  });
  if (recorder.permitted()) {
    const { mask, movementSource } = recorder.getStats();
    try {
      posthog.capture(SESSION_EVENT, { orion_version: VERSION, schema_version: SCHEMA_VERSION, mask,
        pointer_source: movementSource, models: models.map(m => (typeof m === 'string' ? m : m?.name)) });
    } catch { /* the SDK reports its own failures */ }
  }
  return {
    version: VERSION,
    ready,
    load: source => labeller.load(source),
    context: labeller.context,
    contextText: labeller.contextText,
    labels: labeller.labels,
    onLabel: labeller.onLabel,
    flush: recorder.flush,
    pause() { recorder.pause(); labeller.reset(); },
    resume: recorder.resume,
    stop() { labeller.stop(); recorder.stop(); },
    getStats: () => ({ ...recorder.getStats(), live: labeller.stats() }),
  };
}

export const orion = { connect, version: VERSION };
