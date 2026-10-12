import test from 'node:test';
import assert from 'node:assert/strict';
import { createLive, createMood, rankAnswers, MOODS } from '../src/index.js';
import { surveyEntry } from '../src/survey.js';

const HZ = 1000 / 120;
// Feed a pointer path (and presses) to the mood reader the way the live labeller does: records, then 100 ms updates.
function read(path, presses = [], end = 6000) {
  const mood = createMood();
  const rows = [];
  for (let t = 0; t < end; t += HZ) { const p = path(t); if (p) rows.push({ kind: 'pointermove', t, x: p[0], y: p[1] }); }
  for (const t of presses) rows.push({ kind: 'pointerdown', t });
  rows.sort((a, b) => a.t - b.t);
  let i = 0, state;
  for (let now = 0; now <= end; now += 100) {
    while (i < rows.length && rows[i].t <= now) mood.record(rows[i++]);
    state = mood.update(now);
  }
  return state;
}
const wave = (t, hz) => Math.sin(t / 1000 * 2 * Math.PI * hz);

test('the built-in mood reading follows the demo heuristics', () => {
  assert.deepEqual(MOODS, ['angry', 'frustrated', 'calm', 'anxious', 'confused', 'pleased']);
  assert.equal(read(() => null).mood, 'calm', 'no movement reads as calm');
  assert.equal(read(t => [100 + 0.5 * (t % 2000), 300]).mood, 'pleased', 'smooth, direct movement');
  assert.equal(read(t => [400 + 80 * wave(t, 3), 300], [3000, 3600, 4200, 4800, 5400]).mood, 'frustrated', 'side-to-side shaking and repeated clicks');
  assert.equal(read(t => [400 + 300 * wave(t, 1.5), 300 + 40 * Math.sin(t / 90)], [5000, 5200, 5400, 5600, 5800]).mood, 'angry', 'fast, with a burst of clicks');
  assert.equal(read(t => [400 + 0.08 * (t % 3000) + 4 * Math.sin(t / 25), 300 + 4 * Math.cos(t / 31)]).mood, 'anxious', 'small jittery corrections');
  assert.equal(read(t => [400 + 120 * Math.cos(t / 1000 * 2 * Math.PI * 0.8), 300 + 120 * Math.sin(t / 1000 * 2 * Math.PI * 0.8)]).mood, 'confused', 'circling');
  const state = read(() => null);
  assert.ok(Math.abs(Object.values(state.probabilities).reduce((a, b) => a + b, 0) - 1) < 1e-9);
});

function liveClock() {
  let now = 0;
  const timers = [];
  const win = { performance: { now: () => now }, document: { hidden: false }, setInterval: (fn, ms) => { timers.push([fn, ms]); return timers.length; }, clearInterval: () => {} };
  // Advance time, running the 100 ms mood timer like the browser would.
  const run = (to, feed = () => {}) => {
    for (; now < to; now += 100) { feed(now); for (const [fn, ms] of timers) if (ms === 100) fn(); }
  };
  return { win, run, at: () => now };
}

test('mood labels reach PostHog once a mood has held, and only while there is input', async () => {
  const c = liveClock(), sent = [];
  const posthog = { capture: (name, props) => { sent.push([name, props]); return {}; }, get_session_id: () => 'ses-1' };
  const live = createLive({ win: c.win, posthog, permitted: () => true, heartbeat: 60000 });
  assert.equal(await live.load('orion:mood', { agents: ['support-agent'] }), 'orion-mood');
  const shake = t => {
    for (let s = t; s < t + 100; s += HZ) live.record({ kind: 'pointermove', t: s, x: 400 + 80 * wave(s, 3), y: 300 });
    if (t % 600 === 0) live.record({ kind: 'pointerdown', t });
  };
  c.run(1000, t => live.record({ kind: 'pointermove', t, x: 100 + 0.5 * t, y: 300 }));
  c.run(6000, shake);
  const ctx = live.context();
  assert.equal(ctx.model, 'orion-mood');
  assert.equal(ctx.target, 'mood');
  assert.equal(ctx.label, 'frustrated');
  assert.match(ctx.evidence.join('; '), /pointer speed \d+ px\/s; [\d.]+ turns\/s; [\d.]+ clicks\/s/);
  const labels = sent.filter(([n]) => n === 'orion_label').map(([, p]) => p);
  assert.ok(labels.length >= 1 && labels.length <= 4, `a few label events, not one per update (${labels.length})`);
  assert.equal(labels.at(-1).label, 'frustrated');
  assert.deepEqual(labels.at(-1).agents, ['support-agent']);
  const count = labels.length;
  c.run(200000);
  const after = sent.filter(([n]) => n === 'orion_label').map(([, p]) => p).slice(count);
  assert.deepEqual(after.map(p => [p.label, p.changed]), [['calm', true]], 'stillness reads as calm, sent once, and no heartbeats without input');
  assert.equal(live.context().label, 'calm');
});

test('a local mood reading feeds the question but sends nothing and stays out of context', async () => {
  const c = liveClock(), sent = [];
  const live = createLive({ win: c.win, posthog: { capture: (...a) => { sent.push(a); return {}; } }, permitted: () => true });
  await live.load('orion:mood', { local: true });
  c.run(3000, t => live.record({ kind: 'pointermove', t, x: 100 + 0.5 * t, y: 300 }));
  assert.equal(sent.length, 0);
  assert.equal(live.context(), null);
  assert.deepEqual(live.labels(), []);
  assert.ok(live.mood().mood);
  assert.deepEqual(live.stats().models, []);
});

test('self-report answers: two positive and two negative, ranked by the survey file, the mood, or in order', () => {
  const table = { '*': { quiet: ['calm', 'engaged', 'confused', 'bored'] }, '/checkout': { frustrated: ['confident', 'calm', 'frustrated', 'angry'], '*': ['calm', 'pleased', 'anxious', 'confused'] } };
  assert.deepEqual(surveyEntry(table, '/checkout/payment'), table['/checkout']);
  assert.deepEqual(surveyEntry(table, '/about'), table['*']);
  assert.deepEqual(rankAnswers({ table, page: '/checkout/pay', mood: 'frustrated' }),
    { positive: ['confident', 'calm'], negative: ['frustrated', 'angry'], ranked_by: 'survey' });
  assert.deepEqual(rankAnswers({ table, page: '/checkout', mood: 'calm' }).negative, ['anxious', 'confused'], 'falls back to the page entry');
  const probabilities = { angry: 0, frustrated: 0.05, calm: 0.1, anxious: 0.05, confused: 0.5, pleased: 0.3 };
  assert.deepEqual(rankAnswers({ table: null, probabilities }), { positive: ['pleased', 'confident'], negative: ['confused', 'anxious'], ranked_by: 'mood' });
  assert.deepEqual(rankAnswers({}), { positive: ['calm', 'pleased'], negative: ['frustrated', 'confused'], ranked_by: 'default' });
  assert.deepEqual(rankAnswers({ positive: ['calm', 'engaged'], negative: ['confused', 'frustrated'] }).negative, ['confused', 'frustrated']);
});
