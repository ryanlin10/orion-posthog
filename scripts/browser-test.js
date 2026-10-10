import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { serve } from './serve.js';
const server = serve(0);
await new Promise(resolve => server.once('listening', resolve));
const url = `http://127.0.0.1:${server.address().port}`;
const localChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
let browser;
try {
  browser = await chromium.launch({ headless: true, ...(existsSync(localChrome) ? { executablePath: localChrome } : {}) });
  for (const path of ['/artifacts/crowdcast-trace.json', '/.env', '/package.json', '/%2eenv']) {
    assert.equal((await fetch(`${url}${path}`)).status, 403, `Private path served: ${path}`);
  }
  const page = await browser.newPage();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(url); await page.waitForFunction(() => window.recorder);
  await page.locator('#typing').focus();
  await page.keyboard.down('a'); await page.waitForTimeout(25); await page.keyboard.up('a');
  await page.mouse.move(100, 200); await page.mouse.down(); await page.mouse.move(400, 220, { steps: 40 }); await page.mouse.up();
  await page.evaluate(() => recorder.flush());
  let rows = await page.evaluate(() => batches.flatMap(b => b.records));
  for (const kind of ['keydown', 'keyup', 'pointerdown', 'pointerup', 'pointermove', 'click']) assert.ok(rows.some(r => r.kind === kind), `${kind} missing`);
  assert.ok(rows.filter(r => r.kind === 'pointermove').every(r => r.trusted));
  const before = rows.filter(r => r.kind.startsWith('key')).length;
  await page.locator('#secret').focus(); await page.keyboard.type('not-recorded');
  await page.evaluate(() => recorder.flush());
  rows = await page.evaluate(() => batches.flatMap(b => b.records));
  assert.equal(rows.filter(r => r.kind.startsWith('key')).length, before);
  assert.ok(!JSON.stringify(rows).includes('not-recorded'));
  await page.evaluate(() => {
    const host = document.createElement('div'); document.body.append(host);
    host.attachShadow({ mode: 'open' }).innerHTML = '<input type="password">';
    host.shadowRoot.querySelector('input').focus();
  });
  await page.keyboard.type('private');
  const movesBefore = rows.filter(r => r.kind === 'pointermove').length;
  await page.mouse.move(300, 100, { steps: 10 });
  await page.evaluate(() => recorder.flush());
  rows = await page.evaluate(() => batches.flatMap(b => b.records));
  assert.equal(rows.filter(r => r.kind.startsWith('key')).length, before);
  assert.equal(rows.filter(r => r.kind === 'pointermove').length, movesBefore);
  assert.deepEqual(errors, []);

  // Real PostHog SDK, network intercepted locally: verify serialization and session enrichment.
  await page.goto(`${url}/examples/sdk-test.html`);
  const requests = [];
  await page.route('**/ingest/**', async route => {
    requests.push(route.request().postData());
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":1}' });
  });
  await page.addScriptTag({ url: `${url}/node_modules/posthog-js/dist/array.js` });
  await page.evaluate(async () => {
    await new Promise(resolve => posthog.init('phc_local_test_only', { api_host: `${location.origin}/ingest`, persistence: 'memory', autocapture: false,
      capture_pageview: false, capture_pageleave: false, disable_session_recording: true, advanced_disable_flags: true,
      // Test harness runs in headless Chrome; allow it only for this local fixture.
      opt_out_useragent_filter: true, disable_compression: true, request_batching: false, loaded: resolve }));
  });
  await page.addScriptTag({ url: `${url}/dist/recorder.js` });
  await page.waitForFunction(() => window.posthogBehavioral);
  await page.locator('input').focus(); await page.keyboard.type('ab');
  await page.evaluate(() => posthogBehavioral.flush()); await page.waitForTimeout(500);
  const decoded = requests.map(body => {
    if (!body) return '';
    const encoded = new URLSearchParams(body).get('data');
    return encoded ? Buffer.from(encoded, 'base64').toString('utf8') : body;
  });
  const body = decoded.find(s => s.includes('behavioral_burst'));
  if (!body) console.error(JSON.stringify({ requests: requests.map(s => s?.slice(0, 140)), stats: await page.evaluate(() => posthogBehavioral.getStats()), errors }));
  assert.ok(body, 'Real SDK did not submit behavioral_burst');
  const events = decoded.filter(Boolean).flatMap(s => JSON.parse(s));
  const bursts = events.filter(e => e.event === 'behavioral_burst').map(e => e.properties);
  const sdkIdentity = await page.evaluate(() => ({ session: posthog.get_session_id(), person: posthog.get_distinct_id() }));
  assert.ok(bursts.every(b => b.$session_id === sdkIdentity.session && b.distinct_id === sdkIdentity.person));
  assert.ok(bursts.every(b => b.schema_version === '1.0' && typeof b.time_origin_ms === 'number'));
  assert.deepEqual(bursts.flatMap(b => b.records).filter(r => r.kind.startsWith('key')).map(r => [r.kind, r.code]),
    [['keydown', 'KeyA'], ['keyup', 'KeyA'], ['keydown', 'KeyB'], ['keyup', 'KeyB']]);
  assert.deepEqual(errors, []);

  // Exercise the actual DOM listener at synthetic 120/1000/8000 Hz timestamps.
  // CPU time here is a dispatch stress test, never a hardware sampling claim.
  await page.goto(url); await page.waitForFunction(() => window.recorder);
  const stress = await page.evaluate(async () => {
    recorder.stop();
    const { createRecorder } = await import('/src/index.js');
    const results = [];
    for (const hz of [120, 1000, 8000]) {
      let count = 0, mismatches = 0, bytes = 0;
      const sink = { capture(name, b) {
        bytes += new TextEncoder().encode(JSON.stringify(b)).length;
        for (const r of b.records) {
          if (r.kind !== 'pointermove' || r.t !== count * 1000 / hz || r.x !== count) mismatches++;
          count++;
        }
      } };
      const r = createRecorder(sink, { allowSynthetic: true, movementSource: 'pointermove' });
      const start = performance.now(), input = document.querySelector('#typing');
      for (let i = 0; i < hz * 10; i++) {
        const event = new PointerEvent('pointermove', { bubbles: true, clientX: i, pointerId: 1 });
        Object.defineProperty(event, 'timeStamp', { value: i * 1000 / hz });
        input.dispatchEvent(event);
      }
      r.stop();
      results.push({ hz, count, mismatches, bytes, elapsedMs: performance.now() - start, dropped: r.getStats().dropped });
    }
    return results;
  });
  for (const result of stress) {
    assert.equal(result.count, result.hz * 10); assert.equal(result.mismatches, 0); assert.equal(result.dropped, 0);
  }
  console.log(JSON.stringify({ syntheticDispatchStress: stress }));
  if (process.env.CROWDCAST_FIXTURE) {
    const trace = JSON.parse(await readFile(process.env.CROWDCAST_FIXTURE, 'utf8'));
    await page.goto(url); await page.waitForFunction(() => window.recorder);
    const result = await page.evaluate(async trace => {
      recorder.stop(); batches.length = 0;
      const { createRecorder } = await import('/src/index.js');
      window.recorder = createRecorder(mockPosthog, { allowSynthetic: true, movementSource: 'pointermove' });
      const input = document.querySelector('#typing');
      for (const item of trace.events) {
        if (item.type === 'boundary') { recorder.flush(); recorder.pause(); recorder.resume(); continue; }
        const { type, t, ...data } = item;
        const event = type.startsWith('key') ? new KeyboardEvent(type, { ...data, bubbles: true }) : new PointerEvent(type, { ...data, bubbles: true });
        Object.defineProperty(event, 'timeStamp', { value: t });
        input.dispatchEvent(event);
      }
      recorder.flush();
      const rows = batches.flatMap(b => b.records).filter(r => r.kind !== 'boundary');
      return { rows: rows.map(r => ({ type: r.kind, t: r.t })), stats: recorder.getStats() };
    }, trace);
    // Compare source order and timestamps, excluding explicit privacy boundaries.
    const expected = trace.events.filter(e => e.type !== 'boundary').map(e => ({ type: e.type, t: e.t }));
    assert.deepEqual(result.rows, expected);
    assert.equal(result.stats.dropped, 0);
    console.log(JSON.stringify({ crowdcast: 'passed', clock: trace.clock, preservedEvents: expected.length, syntheticFixture: true }));
  }
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ browser: await browser.version(), checks: 'trusted input, pointer down/up, shadow password exclusion, real PostHog SDK payload and identity, script-tag auto-start, private fixture isolation', status: 'passed' }));
} finally { await browser?.close(); await new Promise(resolve => server.close(resolve)); }
