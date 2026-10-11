import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { serve } from './serve.js';
const localChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sdkName = process.env.POSTHOG_SDK ?? 'posthog-js';
if (!['posthog-js', 'posthog-js-current'].includes(sdkName)) throw new Error('Unknown SDK test target');
const sdkScript = await readFile(new URL(`../node_modules/${sdkName}/dist/array.js`, import.meta.url), 'utf8');
const sdkVersion = JSON.parse(await readFile(new URL(`../node_modules/${sdkName}/package.json`, import.meta.url), 'utf8')).version;
const server = serve(0);
await new Promise(resolve => server.once('listening', resolve));
const url = `http://127.0.0.1:${server.address().port}`;
// Both SDK transport envelopes are verified, never silently accepted as zero events.
function transportEvents(body) {
  const data = JSON.parse(body);
  const events = Array.isArray(data) ? data : Array.isArray(data.batch) ? data.batch : [data];
  assert.ok(events.length > 0 && events.every(e => typeof e.event === 'string'));
  return events;
}
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
  await page.addScriptTag({ content: sdkScript });
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
  const events = decoded.filter(Boolean).flatMap(transportEvents);
  const bursts = events.filter(e => e.event === 'behavioral_burst').map(e => e.properties);
  assert.ok(bursts.length > 0, 'SDK payload contains no recorder events');
  const sdkIdentity = await page.evaluate(() => ({ session: posthog.get_session_id(), person: posthog.get_distinct_id() }));
  assert.ok(bursts.every(b => b.$session_id === sdkIdentity.session && b.distinct_id === sdkIdentity.person));
  assert.ok(bursts.every(b => b.schema_version === '1.2' && typeof b.time_origin_ms === 'number' && typeof b.clock.epoch_ms === 'number'));
  assert.deepEqual(bursts.flatMap(b => b.records).filter(r => r.kind.startsWith('key')).map(r => [r.kind, r.code]),
    [['keydown', 'KeyA'], ['keyup', 'KeyA'], ['keydown', 'KeyB'], ['keyup', 'KeyB']]);
  assert.deepEqual(errors, []);

  // Official HTML snippet: recorder.js runs while the snippet placeholder is still window.posthog.
  // The SDK replaces that object when array.js arrives, so the recorder must attach to the loaded instance.
  const snippet = await readFile(new URL('../test/fixtures/posthog-snippet.js', import.meta.url), 'utf8');
  const arrayJs = sdkScript;
  const snippetPage = await browser.newPage(), snippetBodies = [];
  snippetPage.on('pageerror', error => errors.push(error.message));
  await snippetPage.route('**/ingest/**', async route => {
    if (route.request().url().endsWith('/static/array.js')) {
      await new Promise(resolve => setTimeout(resolve, 500));
      return route.fulfill({ contentType: 'text/javascript', body: arrayJs });
    }
    snippetBodies.push(route.request().postData() ?? '');
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
  });
  await snippetPage.goto(`${url}/examples/sdk-test.html`);
  await snippetPage.addScriptTag({ content: `${snippet}
    posthog.init('phc_local_test_only', { api_host: location.origin + '/ingest', persistence: 'memory', autocapture: false,
      capture_pageview: false, capture_pageleave: false, disable_session_recording: true, advanced_disable_flags: true,
      opt_out_useragent_filter: true, disable_compression: true, request_batching: true });` });
  await snippetPage.addScriptTag({ url: `${url}/dist/recorder.js` });
  assert.equal(await snippetPage.evaluate(() => Array.isArray(window.posthog._i)), true, 'SDK loaded before the recorder');
  await snippetPage.waitForFunction(() => window.posthogBehavioral);
  await snippetPage.locator('input').focus(); await snippetPage.keyboard.type('ab');
  await snippetPage.evaluate(() => posthogBehavioral.flush());
  const delivered = () => snippetBodies.some(b => b.includes('behavioral_burst') && b.includes('KeyA'));
  for (let i = 0; i < 50 && !delivered(); i++) await snippetPage.waitForTimeout(100);
  assert.ok(delivered(), 'Snippet install delivered nothing');
  snippetBodies.length = 0;
  await snippetPage.evaluate(() => {
    posthogBehavioral.stop();
    const r = PostHogBehavioral.createRecorder(posthog, { maxEvents: 1, allowSynthetic: true, movementSource: 'pointermove' });
    for (let i = 0; i < 20; i++) document.querySelector('input').dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: i }));
    r.stop();
  });
  await snippetPage.waitForTimeout(300);
  assert.equal(snippetBodies.length, 0, 'Capture options bypassed SDK transport batching');
  for (let i = 0; i < 50 && !snippetBodies.length; i++) await snippetPage.waitForTimeout(100);
  assert.equal(snippetBodies.length, 1, '20 recorder chunks should share one SDK request');
  assert.equal(transportEvents(snippetBodies[0]).length, 20);
  // Recorder batches must neither be refused by, nor drain, the SDK's shared client rate limit (burst 100).
  // This checks SDK acceptance; locally intercepted delivery of every request is slow and not the subject.
  const limited = await snippetPage.evaluate(() => {
    posthogBehavioral.stop();
    const r = PostHogBehavioral.createRecorder(posthog, { maxEvents: 1, allowSynthetic: true, movementSource: 'pointermove' });
    const input = document.querySelector('input');
    for (let i = 0; i < 200; i++) input.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: i, pointerId: 1 }));
    r.stop();
    return { stats: r.getStats(), hostAccepted: posthog.capture('host_event_after_recorder_burst') !== undefined };
  });
  assert.equal(limited.stats.dropped, 0); assert.ok(limited.stats.batches > 100);
  assert.equal(limited.hostAccepted, true, 'Recorder batches starved the host application events');
  await snippetPage.close();
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
  console.log(JSON.stringify({ browser: await browser.version(), sdkVersion, checks: 'trusted input, pointer down/up, shadow password exclusion, real PostHog SDK payload and identity, script-tag auto-start, official snippet install, SDK rate-limit isolation, transport batching, private fixture isolation', status: 'passed' }));
} finally { await browser?.close(); await new Promise(resolve => server.close(resolve)); }
