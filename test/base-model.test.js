// The base model that ships inside orion.js: the browser gives exactly scikit-learn's probabilities on real DUX windows,
// labels follow its decision rule, and connect() runs it live behind a site's own models.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { BASE, BASE_MODEL, connect, createLive, createNormalizer, extractFeatures, predict, validateModel } from '../src/index.js';
import { environment } from './helpers.js';

const WINDOWS = JSON.parse(readFileSync(new URL('./fixtures/base-model-windows.json', import.meta.url), 'utf8'));
const MODEL = JSON.parse(readFileSync(new URL('./fixtures/clicky-model.json', import.meta.url), 'utf8'));

function featuresOf(events) {
  const normalize = createNormalizer();
  const rows = events.map((e, ordinal) => ({ ...normalize(e), ordinal }));
  return extractFeatures(rows, { start_ms: 0, end_ms: BASE_MODEL.window.size_ms }, BASE_MODEL.features.families).features;
}

test('the base model is a valid, documented model of typing rhythm and movement shape, without activity counts', () => {
  validateModel(BASE_MODEL);
  assert.equal(BASE_MODEL.name, 'orion-base');
  assert.deepEqual(BASE_MODEL.target.classes, ['neutral', 'negative']);
  assert.deepEqual(BASE_MODEL.features.families, ['typing', 'motion']);
  assert.ok(!BASE_MODEL.features.names.some(n => /count|_moves$/.test(n)), 'no activity counts');
  assert.equal(BASE_MODEL.estimator.kind, 'linear');
  assert.equal(BASE_MODEL.window.size_ms, 10000);
  assert.deepEqual(BASE_MODEL.decision, { class: 'negative', min_probability: 0.25 });
  assert.match(BASE_MODEL.about.trained_on, /DUX.*CC BY 4\.0/);
  assert.match(BASE_MODEL.about.limits, /Weak/);
  assert.ok(BASE_MODEL.report.validation.p_value <= 0.05 && BASE_MODEL.report.validation.auc > 0.6);
});

test('on real DUX windows the browser gives exactly the probabilities scikit-learn gave', () => {
  assert.equal(WINDOWS.length, 8);
  for (const w of WINDOWS) {
    const result = predict(BASE_MODEL, featuresOf(w.events));
    assert.ok(Math.abs(result.probabilities.negative - w.probability) < 1e-9, `${result.probabilities.negative} vs ${w.probability}`);
    assert.equal(result.label, w.probability >= 0.25 ? 'negative' : 'neutral');
  }
});

test('a decision rule lets a rare class win below one half, and only from its bar', () => {
  const spec = { ...MODEL, estimator: { ...MODEL.estimator, coef: [[0]], intercept: [-1] }, decision: { class: 'yes', min_probability: 0.25 } };
  validateModel(spec);
  const result = predict(spec, { click_interval_ms_median: 1000 }); // p(yes) = sigmoid(-1) = 0.27
  assert.equal(result.label, 'yes');
  assert.ok(Math.abs(result.confidence - 1 / (1 + Math.E)) < 1e-12);
  assert.equal(predict({ ...spec, decision: { class: 'yes', min_probability: 0.3 } }, {}).label, 'no');
  assert.throws(() => validateModel({ ...spec, decision: { class: 'maybe', min_probability: 0.3 } }), /decision rule/);
  assert.throws(() => validateModel({ ...spec, decision: { class: 'yes', min_probability: 0 } }), /decision rule/);
});

test('live: replaying a DUX window through the labeller gives the offline label', async () => {
  for (const w of WINDOWS) {
    let now = 0;
    const live = createLive({ win: { performance: { now: () => now }, setInterval: () => 1, clearInterval: () => {} }, posthog: { capture: () => ({}) },
      permitted: () => true, confirm: 1 });
    await live.load(BASE);
    for (const e of w.events) live.record({ ...e, t: e.t + 1 });
    now = BASE_MODEL.window.size_ms + 1;
    await live.tick();
    assert.equal(live.context('orion-base')?.label, w.probability >= 0.25 ? 'negative' : 'neutral');
    live.stop();
  }
});

test('connect: base: true runs it, it says so at session start, and a site\'s own model comes first in context()', async () => {
  const e = environment();
  e.win.fetch = async () => ({ ok: true, json: async () => MODEL });
  const orion = connect(e.posthog, { window: e.win, allowSynthetic: true, base: { agents: ['support-agent'] }, mood: true, models: ['/orion/clicky.json'] });
  assert.deepEqual((await orion.ready).sort(), ['clicky', 'orion-base']);
  const start = e.calls.find(c => c[0] === 'orion_session_start')[1];
  assert.equal(start.base, true);
  assert.ok(orion.getStats().live.models.includes('orion-base'));
  orion.stop();
  // Priority without a name: the site's model, then the base model, then the mood reading.
  let now = 0;
  const live = createLive({ win: { performance: { now: () => now }, setInterval: () => 1, clearInterval: () => {} }, posthog: { capture: () => ({}) },
    permitted: () => true, confirm: 1 });
  await live.load(BASE);
  for (const ev of WINDOWS[0].events) live.record({ ...ev, t: ev.t + 1 });
  now = 10001; await live.tick();
  assert.equal(live.context().model, 'orion-base');
  await live.load(MODEL);
  for (let t = 6000; t < 10000; t += 300) { live.record({ kind: 'pointerdown', t, pointer_id: 1, button: 0 }); live.record({ kind: 'pointerup', t: t + 80, pointer_id: 1, button: 0 }); }
  now = 10002; await live.tick();
  assert.equal(live.context().model, 'clicky');
  live.stop();
});
