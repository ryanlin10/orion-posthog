import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { serve } from './serve.js';
import { decodeRecords } from '../src/codec.js';
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
function decode(body) {
  if (!body) return '';
  const encoded = new URLSearchParams(body).get('data');
  return encoded ? Buffer.from(encoded, 'base64').toString('utf8') : body;
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
  await page.addScriptTag({ url: `${url}/dist/orion.js` });
  await page.waitForFunction(() => window.orion?.flush);
  await page.locator('input').focus(); await page.keyboard.type('ab');
  await page.evaluate(() => orion.flush()); await page.waitForTimeout(500);
  const decoded = requests.map(decode);
  const body = decoded.find(s => s.includes('orion_input'));
  if (!body) console.error(JSON.stringify({ requests: requests.map(s => s?.slice(0, 140)), stats: await page.evaluate(() => orion.getStats()), errors }));
  assert.ok(body, 'Real SDK did not submit orion_input');
  const events = decoded.filter(Boolean).flatMap(transportEvents);
  const bursts = events.filter(e => e.event === 'orion_input').map(e => e.properties);
  assert.ok(events.some(e => e.event === 'orion_session_start' && e.properties.mask === 'timing'), 'No orion_session_start event');
  assert.ok(bursts.length > 0, 'SDK payload contains no recorder events');
  const sdkIdentity = await page.evaluate(() => ({ session: posthog.get_session_id(), person: posthog.get_distinct_id() }));
  assert.ok(bursts.every(b => b.$session_id === sdkIdentity.session && b.distinct_id === sdkIdentity.person));
  assert.ok(bursts.every(b => b.schema_version === '1.3' && typeof b.time_origin_ms === 'number' && typeof b.clock.epoch_ms === 'number'));
  // Default timing mask: presses pair by opaque ID; no key code, key class or modifier leaves the page.
  const keys = bursts.flatMap(b => decodeRecords(b)).filter(r => r.kind.startsWith('key'));
  assert.deepEqual(keys.map(r => [r.kind, r.press_id]), [['keydown', 1], ['keyup', 1], ['keydown', 2], ['keyup', 2]]);
  assert.ok(keys.every(r => !('code' in r) && !('key_type' in r) && !('modifiers' in r)));
  assert.deepEqual(errors, []);

  // Official HTML snippet: orion.js runs while the snippet placeholder is still window.posthog.
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
  await snippetPage.addScriptTag({ url: `${url}/dist/orion.js` });
  assert.equal(await snippetPage.evaluate(() => Array.isArray(window.posthog._i)), true, 'SDK loaded before the recorder');
  await snippetPage.waitForFunction(() => window.orion?.flush);
  await snippetPage.locator('input').focus(); await snippetPage.keyboard.type('ab');
  await snippetPage.evaluate(() => orion.flush());
  const delivered = () => snippetBodies.some(b => b.includes('orion_input') && b.includes('keydown'));
  for (let i = 0; i < 50 && !delivered(); i++) await snippetPage.waitForTimeout(100);
  assert.ok(delivered(), 'Snippet install delivered nothing');
  snippetBodies.length = 0;
  await snippetPage.evaluate(() => {
    orion.stop();
    const r = Orion.createRecorder(posthog, { maxEvents: 1, allowSynthetic: true, movementSource: 'pointermove' });
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
    const r = Orion.createRecorder(posthog, { maxEvents: 1, allowSynthetic: true, movementSource: 'pointermove' });
    const input = document.querySelector('input');
    for (let i = 0; i < 200; i++) input.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: i, pointerId: 1 }));
    r.stop();
    return { stats: r.getStats(), hostAccepted: posthog.capture('host_event_after_recorder_burst') !== undefined };
  });
  assert.equal(limited.stats.dropped, 0); assert.ok(limited.stats.batches > 100);
  assert.equal(limited.hostAccepted, true, 'Recorder batches starved the host application events');
  await snippetPage.close();
  assert.deepEqual(errors, []);

  // Live labelling: a model served as a static file labels the session in the page, and the label goes to
  // PostHog as an orion_label event tied to the same session.
  const livePage = await browser.newPage(), liveBodies = [];
  livePage.on('pageerror', error => errors.push(error.message));
  await livePage.route('**/ingest/**', async route => {
    liveBodies.push(route.request().postData() ?? '');
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":1}' });
  });
  await livePage.goto(`${url}/examples/sdk-test.html`);
  await livePage.addScriptTag({ content: sdkScript });
  await livePage.evaluate(async () => {
    await new Promise(resolve => posthog.init('phc_local_test_only', { api_host: `${location.origin}/ingest`, persistence: 'memory', autocapture: false,
      capture_pageview: false, capture_pageleave: false, disable_session_recording: true, advanced_disable_flags: true,
      opt_out_useragent_filter: true, disable_compression: true, request_batching: false, loaded: resolve }));
    window.orionOptions = { models: ['/test/fixtures/clicky-model.json'], live: { every: 500, confirm: 1 } };
  });
  await livePage.addScriptTag({ url: `${url}/dist/orion.js` });
  await livePage.waitForFunction(() => window.orion?.ready);
  assert.deepEqual(await livePage.evaluate(() => orion.ready), ['clicky']);
  for (let i = 0; i < 12; i++) { await livePage.mouse.click(200, 60); await livePage.waitForTimeout(200); }
  await livePage.waitForFunction(() => orion.context()?.label === 'yes', null, { timeout: 5000 });
  const live = await livePage.evaluate(() => ({ context: orion.context(), text: orion.contextText(), session: posthog.get_session_id() }));
  assert.equal(live.context.model, 'clicky');
  assert.equal(live.context.session_id, live.session);
  assert.match(live.text, /frustrated: yes/);
  const labelEvents = () => liveBodies.map(decode).filter(b => b.includes('orion_label')).flatMap(transportEvents).filter(e => e.event === 'orion_label');
  for (let i = 0; i < 30 && !labelEvents().length; i++) await livePage.waitForTimeout(100);
  const labelEvent = labelEvents().at(-1);
  assert.ok(labelEvent, 'No orion_label event reached the SDK transport');
  assert.equal(labelEvent.properties.label, 'yes');
  assert.deepEqual(labelEvent.properties.agents, ['support-agent']);
  assert.equal(labelEvent.properties.$session_id, live.session);
  await livePage.close();
  assert.deepEqual(errors, []);

  // Self-report: the question appears while the page is in use, its answer reaches PostHog with the recorder and the
  // moment it was shown, and pointing at or clicking the question never becomes telemetry. Mood runs alongside.
  const reportPage = await browser.newPage();
  reportPage.on('pageerror', error => errors.push(error.message));
  const reportBodies = [];
  await reportPage.route('**/ingest/**', async route => {
    reportBodies.push(route.request().postData() ?? '');
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":1}' });
  });
  await reportPage.goto(`${url}/examples/sdk-test.html`);
  await reportPage.addScriptTag({ content: sdkScript });
  await reportPage.evaluate(async () => {
    await new Promise(resolve => posthog.init('phc_local_test_only', { api_host: `${location.origin}/ingest`, persistence: 'memory', autocapture: false,
      capture_pageview: false, capture_pageleave: false, disable_session_recording: true, advanced_disable_flags: true,
      opt_out_useragent_filter: true, disable_compression: true, request_batching: false, loaded: resolve }));
    window.orionOptions = { mood: true, selfReport: { afterMs: 1500,
      survey: { format: 'orion-survey', table: { '*': { '*': ['calm', 'engaged', 'frustrated', 'confused'] } } } } };
  });
  await reportPage.addScriptTag({ url: `${url}/dist/orion.js` });
  await reportPage.waitForFunction(() => window.orion?.mood);
  for (let i = 0; i < 60 && !(await reportPage.$('[data-orion-self-report]')); i++) {
    await reportPage.mouse.move(100 + (i % 10) * 20, 80 + (i % 3) * 10, { steps: 5 });
    await reportPage.waitForTimeout(100);
  }
  assert.ok(await reportPage.$('[data-orion-self-report]'), 'The self-report question did not appear');
  const offered = await reportPage.evaluate(() => [...document.querySelector('[data-orion-self-report]').shadowRoot.querySelectorAll('button[data-answer]')].map(b => b.dataset.answer));
  assert.deepEqual([...offered].sort(), ['calm', 'confused', 'engaged', 'frustrated']);
  const excludedBefore = await reportPage.evaluate(() => orion.getStats().excluded);
  const choice = await reportPage.evaluateHandle(() => document.querySelector('[data-orion-self-report]').shadowRoot.querySelector('button[data-answer="frustrated"]'));
  await choice.asElement().click();
  await reportPage.waitForFunction(() => !document.querySelector('[data-orion-self-report]'), null, { timeout: 4000 });
  await reportPage.mouse.move(300, 120, { steps: 10 });
  await reportPage.evaluate(() => orion.flush());
  const reportEvents = name => reportBodies.map(decode).filter(b => b.includes(name)).flatMap(transportEvents).filter(e => e.event === name);
  for (let i = 0; i < 30 && !reportEvents('orion_self_report').length; i++) await reportPage.waitForTimeout(100);
  const answer = reportEvents('orion_self_report').at(-1)?.properties;
  assert.ok(answer, 'No orion_self_report event reached the SDK transport');
  assert.equal(answer.answer, 'frustrated');
  assert.equal(answer.dismissed, false);
  assert.equal(answer.ranked_by, 'survey');
  assert.deepEqual(answer.options, offered);
  assert.deepEqual([answer.positive, answer.negative], [['calm', 'engaged'], ['frustrated', 'confused']]);
  assert.ok(answer.answered_t > answer.shown_t && typeof answer.mood === 'string');
  const reportBursts = reportEvents('orion_input').map(e => e.properties);
  assert.ok(reportBursts.length && reportBursts.every(b => b.recorder_id === answer.recorder_id), 'The answer names the recorder whose input it labels');
  const pressesOnQuestion = reportBursts.flatMap(b => decodeRecords(b))
    .filter(r => r.kind.startsWith('pointer') && r.kind !== 'pointermove' && r.t >= answer.shown_t && r.t <= answer.answered_t + 100);
  assert.deepEqual(pressesOnQuestion, [], 'Clicking the question must not become telemetry');
  assert.ok(await reportPage.evaluate(() => orion.getStats().excluded) > excludedBefore);
  assert.ok(reportEvents('orion_session_start').some(e => e.properties.mood === true && e.properties.self_report === true));
  assert.ok(await reportPage.evaluate(() => JSON.parse(localStorage.getItem('orion-self-report')).at > 0), 'The answer starts the cooldown');
  await reportPage.close();
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
      const r = createRecorder(sink, { allowSynthetic: true, movementSource: 'pointermove', recordFormat: 'rows' });
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
      window.recorder = createRecorder(mockPosthog, { allowSynthetic: true, movementSource: 'pointermove', recordFormat: 'rows' });
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
  console.log(JSON.stringify({ browser: await browser.version(), sdkVersion, checks: 'trusted input, pointer down/up, shadow password exclusion, real PostHog SDK payload and identity, script-tag auto-start, official snippet install, live labels from a served model, self-report question and mood reading, SDK rate-limit isolation, transport batching, private fixture isolation', status: 'passed' }));
} finally { await browser?.close(); await new Promise(resolve => server.close(resolve)); }
