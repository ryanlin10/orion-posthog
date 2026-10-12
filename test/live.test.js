import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { connect, createLive, keyType, fieldType } from '../src/index.js';
import { decodeRecords } from '../src/codec.js';
import { environment } from './helpers.js';

// p(yes) = sigmoid(5 - 0.01 × median click interval): quick repeated clicks read as "yes".
const MODEL = JSON.parse(readFileSync(new URL('./fixtures/clicky-model.json', import.meta.url), 'utf8'));

function clock() {
  let now = 0;
  const win = { performance: { now: () => now }, setInterval: () => 1, clearInterval: () => {} };
  return { win, set: t => { now = t; } };
}
function clicks(live, from, to, every) {
  for (let t = from; t < to; t += every) {
    live.record({ kind: 'pointerdown', t, pointer_id: 1, button: 0 });
    live.record({ kind: 'pointerup', t: t + 80, pointer_id: 1, button: 0 });
  }
}

test('live labels need a full window, switch only after confirmation, and carry evidence', async () => {
  const c = clock(), sent = [];
  const posthog = { capture: (name, props) => { sent.push([name, props]); return {}; }, get_session_id: () => 'ses-1' };
  const live = createLive({ win: c.win, posthog, permitted: () => true, every: 1000, confirm: 2 });
  await live.load(MODEL);
  c.set(500); live.tick();
  assert.equal(live.context(), null, 'no label before any input');
  clicks(live, 0, 6000, 2000);
  c.set(6000); live.tick();
  assert.equal(live.context().label, 'no');
  clicks(live, 6000, 12000, 250);
  c.set(9000); live.tick();
  assert.equal(live.context().label, 'no', 'one tick is not enough to switch');
  c.set(10000); live.tick();
  const ctx = live.context();
  assert.equal(ctx.label, 'yes');
  assert.deepEqual(ctx.earlier, ['no']);
  assert.equal(ctx.session_id, 'ses-1');
  assert.match(ctx.evidence[0], /time between clicks lower than usual \(250 ms vs 1,500 ms\)/);
  assert.deepEqual(sent.map(([n, p]) => [n, p.label, p.changed]), [['orion_label', 'no', true], ['orion_label', 'yes', true]]);
  assert.deepEqual(sent[1][1].agents, ['support-agent']);
  assert.match(live.contextText(), /# from Orion · clicky v3\nuser_state:\n {2}frustrated: yes\n {2}confidence: 0\.92/);
});

test('gaps, consent and resets stop live labelling', async () => {
  const c = clock(), sent = [];
  let allowed = true;
  const posthog = { capture: (name, props) => { sent.push(props); return {}; } };
  const live = createLive({ win: c.win, posthog, permitted: () => allowed, every: 1000, confirm: 1 });
  await live.load(MODEL);
  clicks(live, 0, 3000, 250);
  live.record({ kind: 'boundary', t: 3100, reason: 'blur' });
  clicks(live, 3200, 5000, 250);
  c.set(5000); live.tick();
  assert.equal(live.context(), null, 'a window containing a capture gap is not scored');
  assert.equal(live.stats().noReadReasons.clicky, 'capture-gap');
  c.set(16000); live.tick();
  assert.equal(live.stats().noReadReasons.clicky, 'too-few-events');
  clicks(live, 16000, 20000, 250);
  allowed = false;
  c.set(20000); live.tick();
  assert.equal(sent.length, 0, 'nothing is sent without consent');
  assert.equal(live.context(), null);
  allowed = true; live.tick();
  assert.equal(live.context().label, 'yes');
  assert.equal(sent.length, 1);
  live.reset();
  assert.equal(live.context(), null, 'a reset forgets labels as well as input');
});

test('connect: timing mask by default, key types on request, focus fields, session start event', () => {
  const e = environment();
  const orion = connect(e.posthog, { window: e.win, allowSynthetic: true });
  e.emit('keydown', { code: 'KeyA', timeStamp: 10 });
  e.emit('keyup', { code: 'KeyA', timeStamp: 90 });
  const input = { tagName: 'INPUT', type: 'email', matches: () => false, closest: () => null };
  e.emit('focusin', { timeStamp: 100, target: input, composedPath: () => [input] });
  orion.flush();
  assert.equal(e.calls[0][0], 'orion_session_start');
  assert.equal(e.calls[0][1].mask, 'timing');
  const batch = e.calls.find(c => c[0] === 'orion_input')[1];
  const rows = decodeRecords(batch);
  assert.deepEqual(rows.map(r => [r.kind, r.press_id ?? r.field]), [['keydown', 1], ['keyup', 1], ['focusin', 'text']]);
  assert.ok(rows.every(r => !('code' in r) && !('key_type' in r) && !('modifiers' in r)));
  assert.equal(batch.schema_version, '1.3');
  orion.stop();

  const typed = connect(e.posthog, { window: e.win, allowSynthetic: true, mask: 'types' });
  e.emit('keydown', { code: 'Backspace', timeStamp: 200 });
  typed.flush();
  const last = decodeRecords(e.calls.at(-1)[1]);
  assert.equal(last.at(-1).key_type, 'delete');
  assert.ok(!('code' in last.at(-1)));
  typed.stop();
  assert.throws(() => connect(e.posthog, { window: e.win, mask: 'letters' }), /Invalid mask/);
});

test('connect: models load from URLs and labels clear on pause', async () => {
  const e = environment();
  e.win.fetch = async url => ({ ok: url === '/orion/clicky.json', status: 404, json: async () => MODEL });
  const errors = [];
  const original = console.error;
  console.error = (...args) => errors.push(args.join(' '));
  try {
    const orion = connect(e.posthog, { window: e.win, allowSynthetic: true, models: ['/orion/clicky.json', '/orion/missing.json'] });
    assert.deepEqual(await orion.ready, ['clicky']);
    assert.match(errors[0], /missing\.json.*HTTP 404/);
    assert.deepEqual(orion.getStats().live.models, ['clicky']);
    orion.pause();
    assert.equal(orion.context(), null);
    orion.stop();
  } finally {
    console.error = original;
  }
});

test('key classes and field kinds never carry identities', () => {
  assert.deepEqual(['KeyQ', 'Digit7', 'Space', 'Delete', 'NumpadEnter', 'ShiftLeft', 'ArrowUp', 'PageDown', 'Comma'].map(keyType),
    ['letter', 'digit', 'space', 'delete', 'enter', 'modifier', 'navigation', 'navigation', 'other']);
  assert.deepEqual([{ tagName: 'TEXTAREA' }, { tagName: 'INPUT', type: 'checkbox' }, { tagName: 'INPUT', type: 'submit' }, { tagName: 'DIV', isContentEditable: true }, null].map(fieldType),
    ['textarea', 'choice', 'button', 'editable', 'other']);
});

test('a score of 0 is a label, not the absence of one', async () => {
  const c = clock(), sent = [];
  const posthog = { capture: (name, props) => { sent.push([name, props]); return {}; } };
  const live = createLive({ win: c.win, posthog, permitted: () => true, every: 1000 });
  const score = { ...MODEL, name: 'calm-score', target: { name: 'calm', type: 'continuous', min: 0, max: 1 },
    estimator: { kind: 'linear', coef: [[0]], intercept: [0] } };
  await live.load(score);
  clicks(live, 0, 6000, 2000);
  c.set(6000); live.tick();
  assert.equal(live.context()?.label, 0);
  assert.match(live.contextText(), /calm/);
  assert.equal(sent.filter(([name]) => name === 'orion_label').at(-1)?.[1].label, 0);
});

test('connect: a second call for the same PostHog returns the running connection until it stops', () => {
  const e = environment();
  const first = connect(e.posthog, { window: e.win, allowSynthetic: true });
  const again = connect(e.posthog, { window: e.win, allowSynthetic: true, mask: 'types' });
  assert.equal(again, first);
  assert.equal(e.calls.filter(c => c[0] === 'orion_session_start').length, 1, 'one session start, not two');
  first.stop();
  const fresh = connect(e.posthog, { window: e.win, allowSynthetic: true });
  assert.notEqual(fresh, first);
  fresh.stop();
});
