// Schema 1.4 records (field fills, returns to the page, scroll extents, target boxes) and the live rules that use them:
// time away that does not stop scoring, per-person baselines kept in the browser, and the mask the correction rate needs.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { connect, createLive, createRecorder, fillVia } from '../src/index.js';
import { decodeRecords } from '../src/codec.js';
import { environment } from './helpers.js';

const MODEL = JSON.parse(readFileSync(new URL('./fixtures/clicky-model.json', import.meta.url), 'utf8'));
const field = (tagName, type = 'text') => ({ tagName, type, matches: () => false, closest: () => null });

function setup(t, options = {}) {
  const e = environment();
  let now = 0;
  e.win.performance.now = () => now;
  const recorder = createRecorder(e.posthog, { window: e.win, allowSynthetic: true, ...options });
  t.after(() => recorder.stop());
  return { ...e, recorder, at: v => { now = v; }, rows: () => e.calls.filter(c => c[0] === 'orion_input').flatMap(c => decodeRecords(c[1])) };
}

test('a field that changes without typing is recorded with how, never what', t => {
  const e = setup(t);
  const on = node => ({ target: node, composedPath: () => [node] });
  const text = field('INPUT');
  e.emit('input', { inputType: 'insertFromPaste', ...on(text), timeStamp: 10, data: 'secret' });
  e.emit('input', { inputType: 'insertText', ...on(text), timeStamp: 20, data: 'a' });
  e.emit('input', { inputType: 'deleteContentBackward', ...on(text), timeStamp: 30 });
  e.emit('input', { ...on(text), timeStamp: 40 }); // Autofill fires input with no inputType.
  e.emit('input', { ...on(field('INPUT', 'checkbox')), timeStamp: 50 });
  e.emit('input', { inputType: 'insertFromDrop', ...on(field('TEXTAREA')), timeStamp: 60 });
  e.recorder.flush();
  assert.deepEqual(e.rows().map(r => [r.kind, r.field, r.via, r.t]), [['input', 'text', 'paste', 10], ['input', 'text', 'fill', 40], ['input', 'textarea', 'drop', 60]]);
  assert.ok(e.rows().every(r => !('data' in r) && !('value' in r)));
  assert.deepEqual([fillVia('historyUndo', 'text'), fillVia('insertReplacementText', 'text'), fillVia(undefined, 'select')], [null, 'fill', null]);
});

test('fills into private fields are excluded, typed input there is not even counted, and fills: false turns it off', t => {
  const e = setup(t);
  const secret = { ...field('INPUT'), matches: () => true };
  e.emit('input', { inputType: 'insertText', target: secret, composedPath: () => [secret] });
  assert.equal(e.recorder.getStats().excluded, 0);
  e.emit('input', { inputType: 'insertFromPaste', target: secret, composedPath: () => [secret] });
  assert.equal(e.recorder.getStats().excluded, 1);
  const off = setup(t, { fills: false });
  const text = field('INPUT');
  off.emit('input', { inputType: 'insertFromPaste', target: text, composedPath: () => [text] });
  off.recorder.flush();
  assert.equal(off.rows().length, 0);
});

test('returning to the page records how long it was hidden, sent with the next input', t => {
  const e = setup(t);
  e.at(1000); e.emit('pointermove', { timeStamp: 1000, clientX: 1, clientY: 1 });
  e.at(1200); e.doc.hidden = true; e.emit('visibilitychange'); // Input buffered: the boundary keeps its own time.
  e.at(6200); e.doc.hidden = false; e.emit('visibilitychange');
  assert.equal(e.calls.filter(c => c[0] === 'orion_input').length, 1, 'a return alone sends nothing');
  e.at(6900); e.emit('pointermove', { timeStamp: 6900, clientX: 2, clientY: 2 });
  e.at(7000); e.doc.hidden = true; e.emit('visibilitychange'); // Nothing buffered after this flush: the boundary waits.
  e.at(9000); e.doc.hidden = false; e.emit('visibilitychange');
  e.at(9500); e.emit('keydown', { code: 'KeyA', timeStamp: 9500 });
  e.recorder.flush();
  assert.deepEqual(e.rows().map(r => [r.kind, r.t, r.reason ?? r.hidden_ms]),
    [['pointermove', 1000, undefined], ['boundary', 1200, 'hidden'], ['visible', 6200, 5000], ['pointermove', 6900, undefined],
      ['boundary', 7000, 'hidden'], ['visible', 9000, 2000], ['keydown', 9500, undefined]]);
});

test('scrolls carry the scroller extent when it is the page or a large area, and presses carry the control box', t => {
  const e = setup(t);
  const page = { scrollHeight: 5000, clientHeight: 880 };
  Object.assign(e.doc, { scrollingElement: page, documentElement: page });
  e.win.scrollY = 1200;
  e.emit('scroll', { target: e.doc, timeStamp: 5 });
  e.emit('scroll', { target: { scrollTop: 40, scrollHeight: 600, clientHeight: 200, matches: () => false, closest: () => null }, timeStamp: 6 });
  const button = { matches: s => s.includes('button'), getBoundingClientRect: () => ({ left: 100.4, top: 50.6, width: 80, height: 32 }) };
  const label = { matches: () => false };
  e.emit('pointerdown', { timeStamp: 7, clientX: 120, clientY: 60, composedPath: () => [label, button], target: label });
  e.emit('pointerdown', { timeStamp: 8, clientX: 5, clientY: 5 });
  e.recorder.flush();
  const [pageScroll, inner, aimed, loose] = e.rows();
  assert.deepEqual([pageScroll.y, pageScroll.content_height, pageScroll.view_height], [1200, 5000, 880]);
  assert.ok(!('content_height' in inner), 'a small scrolling area is not the page');
  assert.deepEqual([aimed.box_x, aimed.box_y, aimed.box_w, aimed.box_h], [100, 51, 80, 32]);
  assert.ok(!('box_x' in loose));
  assert.equal(e.calls.find(c => c[0] === 'orion_input')[1].viewport.doc_height, 5000);
});

test('with allow_away a window that holds time away is still scored; without it, it is not', async () => {
  for (const away of [true, false]) {
    let now = 0;
    const win = { performance: { now: () => now }, setInterval: () => 1, clearInterval: () => {} };
    const live = createLive({ win, posthog: { capture: () => ({}) }, permitted: () => true, every: 1000, confirm: 1 });
    await live.load({ ...MODEL, window: { ...MODEL.window, allow_away: away } });
    for (let t = 0; t < 1500; t += 250) { live.record({ kind: 'pointerdown', t, pointer_id: 1, button: 0 }); live.record({ kind: 'pointerup', t: t + 80, pointer_id: 1, button: 0 }); }
    live.record({ kind: 'boundary', t: 1600, reason: 'hidden' });
    live.record({ kind: 'visible', t: 2600, hidden_ms: 1000 });
    for (let t = 2700; t < 4000; t += 250) { live.record({ kind: 'pointerdown', t, pointer_id: 1, button: 0 }); live.record({ kind: 'pointerup', t: t + 80, pointer_id: 1, button: 0 }); }
    now = 4000; await live.tick();
    assert.equal(live.context()?.label ?? null, away ? 'yes' : null);
    assert.equal(live.stats().noReadReasons.clicky, away ? null : 'capture-gap');
  }
});

test('baselines: closed tiles reach the browser store, and the next page load scores against them', async () => {
  const storage = new Map();
  const win = () => ({ performance: { now: () => 0 }, setInterval: () => 1, clearInterval: () => {},
    localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v)) } });
  const spec = { ...MODEL, features: { ...MODEL.features, names: ['click_interval_ms_median', 'click_interval_ms_median_rel'],
    baseline: { tile_ms: 4000, grace_ms: 1000, cap: 100, min_tiles: 2 } },
    preprocess: { impute: [0, 0], indicators: [], mean: [0, 0], scale: [1, 1] }, estimator: { ...MODEL.estimator, coef: [[0, 0]] } };
  const first = createLive({ win: win(), posthog: { capture: () => ({}) }, permitted: () => true });
  await first.load(spec);
  for (let t = 0; t < 13000; t += 500) first.record({ kind: 'pointerdown', t, pointer_id: 1, button: 0 });
  const saved = JSON.parse(storage.get('orion-baseline:clicky'));
  assert.deepEqual(saved.means.click_interval_ms_median, [500, 2], 'two tiles closed: [0, 4 s) and [4, 8 s)');
  assert.ok(!JSON.stringify(saved).includes('pointerdown'), 'the store holds averages, not input');
  first.stop();
  const second = createLive({ win: win(), posthog: { capture: () => ({}) }, permitted: () => true });
  await second.load(spec);
  const { relative } = await import('../src/baseline.js');
  assert.deepEqual(relative({ click_interval_ms_median: 300 }, saved.means, 2), { click_interval_ms_median_rel: -200 });
  second.stop();
});

test('connect warns when a model reads corrections but the mask hides key classes', async () => {
  const e = environment();
  const spec = { ...MODEL, features: { ...MODEL.features, families: ['pointer', 'corrections'] } };
  const warnings = [];
  const original = console.warn;
  console.warn = (...args) => warnings.push(args.join(' '));
  try {
    const orion = connect(e.posthog, { window: e.win, allowSynthetic: true, models: [spec] });
    await orion.ready;
    orion.stop();
    const typed = connect(e.posthog, { window: e.win, allowSynthetic: true, mask: 'types', models: [spec] });
    await typed.ready;
    typed.stop();
  } finally {
    console.warn = original;
  }
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /clicky reads corrections, which need mask: 'types'/);
});
