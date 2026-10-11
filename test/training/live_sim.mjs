// Replays recorded input through the browser's live labeller on a simulated clock.
// usage: node live_sim.mjs model.json sessions.json → JSON { sessions: [{ session_id, ticks: [{ t, label, confidence }] }], events }
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { createLive } from '../../src/live.js';

const spec = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const sessions = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const out = { sessions: [], events: [] };
for (const s of sessions) {
  let now = 0;
  const win = { performance: { now: () => now }, setInterval: () => 1, clearInterval: () => {} };
  const posthog = { capture: (name, props) => { out.events.push({ name, session_id: s.session_id, t: now, props }); return {}; },
    get_session_id: () => s.session_id };
  const live = createLive({ win, posthog, permitted: () => true, every: spec.live.every_ms, heartbeat: spec.live.heartbeat_ms, confirm: spec.live.confirm });
  if (spec.estimator.kind === 'onnx') {
    const ort = await import('onnxruntime-web');
    const session = await ort.InferenceSession.create(readFileSync(join(dirname(process.argv[2]), spec.estimator.file)));
    await live.load(spec, { session, ort });
  } else await live.load(spec);
  const ticks = [];
  let next = s.records[0].t + spec.live.every_ms;
  for (const r of s.records) {
    while (r.t >= next) {
      now = next; await live.tick();
      const c = live.context();
      ticks.push({ t: now, label: c?.label ?? null, confidence: c?.confidence ?? null });
      next += spec.live.every_ms;
    }
    now = r.t; live.record(r);
  }
  out.sessions.push({ session_id: s.session_id, ticks, stats: live.stats() });
  live.stop();
}
console.log(JSON.stringify(out));
