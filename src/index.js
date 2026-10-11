import { encodeRecords } from './codec.js';
export const SCHEMA_VERSION = '1.2';
export const EVENT_NAME = 'behavioral_burst';
const PRIVATE = 'input[type="password"],[data-ph-behavior-ignore],[data-private],.ph-no-capture,.ph-sensitive';
const instances = new WeakMap();
// UTF-8 length of JSON text without allocating an encoded copy. JSON.stringify escapes lone surrogates.
function utf8Length(text) {
  let n = text.length;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c >= 0x80) n += c < 0x800 || (c >= 0xd800 && c < 0xe000) ? 1 : 2;
  }
  return n;
}
const bytes = value => utf8Length(JSON.stringify(value));
const sameIdentity = (a, b) => a && b && a[0] === b[0] && a[1] === b[1];
const finite = (n, fallback = 0) => Number.isFinite(n) ? n : fallback;
// randomUUID exists only in secure contexts; getRandomValues also works on plain HTTP pages.
function uuid(crypto) {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/** Capture actual browser samples only. No polling, interpolation or predicted events. */
export function createRecorder(posthog, options = {}) {
  const win = options.window ?? globalThis.window;
  if (!win?.document) throw new Error('createRecorder requires a browser window');
  if (typeof posthog?.capture !== 'function') throw new TypeError('A loaded PostHog SDK is required');
  // The HTML snippet's placeholder only queues calls in `_i`; the loaded SDK replaces that object entirely.
  if (Array.isArray(posthog._i)) throw new TypeError('Pass the loaded PostHog SDK, not the snippet placeholder');
  if (instances.has(win)) throw new Error('A recorder is already running in this window');
  const doc = win.document;
  const config = {
    idleMs: options.idleMs ?? 500,
    flushMs: options.flushMs ?? 1000,
    maxEvents: options.maxEvents ?? 128,
    maxBytes: options.maxBytes ?? 48000,
    keyboard: options.keyboard ?? 'code',
    movementSource: options.movementSource ?? 'auto',
    allowSynthetic: options.allowSynthetic ?? false,
    isAllowed: options.isAllowed ?? (() => true),
    recordFormat: options.recordFormat ?? 'rows',
    maxBytesPerMinute: options.maxBytesPerMinute ?? 0,
    maxTotalBytes: options.maxTotalBytes ?? 0,
  };
  for (const [key, min, max] of [['idleMs', 50, 60000], ['flushMs', 50, 60000], ['maxEvents', 1, 10000], ['maxBytes', 4096, 48000]]) {
    if (!Number.isInteger(config[key]) || config[key] < min || config[key] > max) throw new RangeError(`Invalid ${key}`);
  }
  if (!['code', 'timing', 'off'].includes(config.keyboard)) throw new RangeError('Invalid keyboard mode');
  if (!['rows', 'columns'].includes(config.recordFormat)) throw new RangeError('Invalid recordFormat');
  for (const key of ['maxBytesPerMinute', 'maxTotalBytes']) {
    if (!Number.isSafeInteger(config[key]) || config[key] < 0) throw new RangeError(`Invalid ${key}`);
  }
  if (!['auto', 'pointermove', 'pointerrawupdate'].includes(config.movementSource)) throw new RangeError('Invalid movementSource');
  const excluded = options.excludeSelector ? `${PRIVATE},${options.excludeSelector}` : PRIVATE;
  doc.querySelector(excluded); // Validate before attaching any listeners.
  const origin = win.performance.timeOrigin;
  const id = uuid(win.crypto);
  const raw = 'onpointerrawupdate' in win;
  const source = config.movementSource === 'auto' ? (raw ? 'pointerrawupdate' : 'pointermove') : config.movementSource;
  // posthog-js returns undefined from capture() when it refuses an event; wrappers and mocks may return nothing.
  const reportsRefusals = typeof posthog.__loaded === 'boolean';
  const stats = { recorded: 0, submitted: 0, batches: 0, dropped: 0, excluded: 0, errors: 0, refusedBatches: 0, budgetDropped: 0, attemptedBytes: 0 };
  const listeners = [];
  const targets = new WeakMap();
  let nextTarget = 1, records = [], recordBytes = 0, identity = null;
  let burst = 0, chunk = 0, sequence = 0, lastT = null, lastActivity = 0, idleTimer, flushTimer;
  let stopped = false, paused = false, pendingBoundary = null;
  let generation = 0, nextPress = 1, minuteStart = win.performance.now(), minuteBytes = 0;
  const pressed = new Map();
  function markGap(reason) { pendingBoundary = reason; pressed.clear(); }

  function permitted() {
    try { return !paused && config.isAllowed() && posthog.has_opted_out_capturing?.() !== true; }
    catch { return false; }
  }
  function currentIdentity() {
    try { return [posthog.get_session_id?.() ?? null, posthog.get_distinct_id?.() ?? null]; }
    catch { stats.errors++; return null; }
  }
  function clearTimers() {
    win.clearTimeout(idleTimer); win.clearTimeout(flushTimer);
    idleTimer = flushTimer = undefined;
  }
  function discard() {
    generation++;
    pressed.clear();
    if (stats.recorded) markGap('capture-gap');
    stats.dropped += records.length;
    records = []; recordBytes = 0; lastT = null; chunk = 0;
    clearTimers();
  }
  function flush(reason = 'manual') {
    if (!records.length) return;
    if (!permitted() || !sameIdentity(currentIdentity(), identity)) { discard(); return; }
    let start = Infinity, end = -Infinity;
    for (const { t } of records) { if (t < start) start = t; if (t > end) end = t; }
    const batch = {
      schema_version: SCHEMA_VERSION, recorder_id: id, burst_id: `${id}:${burst}`,
      chunk_index: chunk++, batch_sequence: sequence++, reason,
      time_origin_ms: origin, start_ms: start, end_ms: end,
      // Wall-clock anchor: timeOrigin + t can drift from Date.now(), which replay timestamps use.
      clock: { epoch_ms: Date.now(), perf_ms: win.performance.now() },
      ...(identity[0] !== null ? { $session_id: identity[0] } : {}),
      viewport: { width: win.innerWidth, height: win.innerHeight, dpr: win.devicePixelRatio, scroll_x: win.scrollX, scroll_y: win.scrollY },
      capture: { movement_source: source, coalesced_supported: typeof win.PointerEvent?.prototype.getCoalescedEvents === 'function', keyboard: config.keyboard, target_hz: 120, synthetic_allowed: config.allowSynthetic },
      quality: { record_count: records.length, dropped_before_batch: stats.dropped, excluded_events_total: stats.excluded, sdk_refused_batches: stats.refusedBatches },
      ...(config.recordFormat === 'columns' ? { record_columns: encodeRecords(records) } : { records }),
    };
    const count = records.length;
    records = []; recordBytes = 0;
    win.clearTimeout(flushTimer); flushTimer = undefined;
    try {
      const size = bytes(batch);
      if (size > config.maxBytes) throw new Error('Batch exceeds configured byte limit');
      const now = win.performance.now();
      if (now - minuteStart >= 60000) { minuteStart = now; minuteBytes = 0; }
      if ((config.maxBytesPerMinute && minuteBytes + size > config.maxBytesPerMinute) ||
          (config.maxTotalBytes && stats.attemptedBytes + size > config.maxTotalBytes)) {
        stats.dropped += count; stats.budgetDropped += count; markGap('budget'); return;
      }
      minuteBytes += size; stats.attemptedBytes += size;
      // Skip the SDK's client rate limit: its token bucket is shared, so recorder batches would otherwise
      // be dropped and also starve the host application's own events.
      const beacon = reason === 'hidden' || reason === 'pagehide';
      // _batchKey is a version-tested SDK adapter detail: capture options otherwise bypass its queue.
      const result = posthog.capture(EVENT_NAME, batch, { skip_client_rate_limiting: true,
        ...(beacon ? { transport: 'sendBeacon', send_instantly: true } : { _batchKey: EVENT_NAME }) });
      // Handing the batch to the SDK is not a server ACK. Undefined means the SDK refused it
      // (opt-out, before_send, bot filter, or not initialized).
      if (reportsRefusals && result === undefined) { stats.dropped += count; stats.refusedBatches++; markGap('capture-gap'); }
      else { stats.submitted += count; stats.batches++; }
    } catch { stats.dropped += count; stats.errors++; markGap('capture-gap'); }
  }
  // Permission and identity are checked once per dispatched event, not once per coalesced sample.
  function admit(t) {
    if (stopped || paused) return false;
    if (!permitted()) { discard(); return false; }
    const nextIdentity = currentIdentity();
    if (!nextIdentity) { discard(); return false; }
    const changed = identity && !sameIdentity(identity, nextIdentity);
    if (changed) discard();
    identity = nextIdentity;
    if (changed) markGap('identity');
    const admittedGeneration = generation;
    if (pendingBoundary) {
      const reason = pendingBoundary; pendingBoundary = null;
      append({ kind: 'boundary', t, reason });
    }
    return !stopped && !paused && generation === admittedGeneration;
  }
  function push(record) {
    if (admit(record.t)) append(record);
  }
  function append(record) {
    if (stopped || paused) return;
    const admittedGeneration = generation;
    if (lastT === null || record.t - lastT > config.idleMs) {
      flush('idle'); burst++; chunk = 0;
    }
    lastT = Math.max(lastT ?? record.t, record.t);
    const size = bytes(record) + 1;
    if (size > config.maxBytes - 2048) { stats.dropped++; markGap('capture-gap'); return; }
    if (records.length >= config.maxEvents || recordBytes + size > config.maxBytes - 2048) flush('limit');
    // capture() invokes application hooks synchronously; they may stop, pause or reset us.
    if (stopped || paused || generation !== admittedGeneration) return;
    records.push(record); recordBytes += size; stats.recorded++;
    if (flushTimer === undefined) flushTimer = win.setTimeout(() => flush('interval'), config.flushMs);
    // One idle timer per burst that re-arms for the remaining time, instead of a new timer per sample.
    lastActivity = win.performance.now();
    if (idleTimer === undefined) idleTimer = win.setTimeout(idle, config.idleMs);
  }
  function idle() {
    const remaining = lastActivity + config.idleMs - win.performance.now();
    if (remaining > 0) { idleTimer = win.setTimeout(idle, remaining); return; }
    idleTimer = undefined;
    flush('idle'); lastT = null; chunk = 0;
  }
  function timestamp(e) {
    // Older implementations may expose epoch rather than performance-relative timestamps.
    const t = finite(e.timeStamp, win.performance.now());
    return t > 1e12 ? t - origin : t;
  }
  function privateTarget(e) {
    // composedPath lists every ancestor, across open shadow roots, so matches() per node is enough.
    const path = e.composedPath?.();
    if (path?.length ? path.some(node => node?.matches?.(excluded)) : e.target?.closest?.(excluded)) return true;
    for (let node = doc.activeElement; node; node = node.shadowRoot?.activeElement) {
      if (node.matches?.(excluded) || node.closest?.(excluded)) return true;
    }
    return false;
  }
  function targetId(e) {
    const node = e.composedPath?.()[0] ?? e.target;
    if (!node || typeof node !== 'object') return 0;
    if (!targets.has(node)) targets.set(node, nextTarget++);
    return targets.get(node);
  }
  function base(e, kind, target = targetId(e)) {
    return { kind, t: timestamp(e), trusted: e.isTrusted === true, target };
  }
  const modifiers = e => (e.shiftKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.altKey ? 4 : 0) | (e.metaKey ? 8 : 0);
  function keyboard(e) {
    if (config.keyboard === 'off') return;
    const record = { ...base(e, e.type), repeat: e.repeat, composing: e.isComposing };
    if (!admit(record.t)) return;
    const key = `${e.code ?? ''}:${e.location ?? 0}`;
    if (e.type === 'keydown' && !e.repeat && e.code) pressed.set(key, nextPress++);
    if (pressed.has(key)) record.press_id = pressed.get(key);
    if (e.type === 'keyup') pressed.delete(key);
    if (config.keyboard === 'code') Object.assign(record, { code: String(e.code ?? '').slice(0, 40), location: e.location, modifiers: modifiers(e) });
    append(record);
  }
  function pointer(e, parent = e, target) {
    return { ...base(parent, parent.type === source ? 'pointermove' : parent.type, target), t: timestamp(e), pointer_id: e.pointerId,
      pointer_type: e.pointerType, x: finite(e.clientX), y: finite(e.clientY), page_x: finite(e.pageX), page_y: finite(e.pageY),
      dx: finite(e.movementX), dy: finite(e.movementY), button: e.button, buttons: e.buttons,
      pressure: finite(e.pressure), tilt_x: finite(e.tiltX), tilt_y: finite(e.tiltY), twist: finite(e.twist), modifiers: modifiers(parent) };
  }
  function movement(e) {
    // Use the coalesced list OR its parent. Including both duplicates the final sample.
    let samples;
    try { samples = e.getCoalescedEvents?.(); } catch { /* unsupported implementation */ }
    const list = samples?.length ? samples : [e];
    if (!admit(timestamp(list[0]))) return;
    const target = targetId(e), coalesced = !!samples?.length;
    const admittedGeneration = generation;
    for (const sample of list) {
      if (stopped || paused || generation !== admittedGeneration) break;
      append({ ...pointer(sample, e, target), coalesced });
    }
  }
  function listen(target, type, fn, filter = true) {
    const handler = e => {
      try {
        if (stopped) return;
        if (filter && (!config.allowSynthetic && !e.isTrusted)) return;
        if (filter && privateTarget(e)) {
          stats.excluded++;
          if (stats.recorded) markGap('excluded');
          return;
        }
        fn(e);
      } catch { stats.errors++; }
    };
    target.addEventListener(type, handler, { capture: true, passive: true });
    listeners.push(() => target.removeEventListener(type, handler, true));
  }
  function boundary(reason) {
    pressed.clear();
    // With input buffered, close it at the boundary's own time. Otherwise carry the boundary to the next
    // permitted record rather than submitting an event that contains no input.
    if (records.length && permitted()) push({ kind: 'boundary', t: win.performance.now(), reason });
    else if (stats.recorded) markGap(reason);
    flush(reason); lastT = null;
  }
  listen(doc, 'keydown', keyboard); listen(doc, 'keyup', keyboard);
  listen(doc, source, movement);
  for (const type of ['pointerdown', 'pointerup', 'pointercancel', 'click', 'dblclick', 'contextmenu']) listen(doc, type, e => push(pointer(e)));
  listen(doc, 'wheel', e => push({ ...base(e, 'wheel'), dx: e.deltaX, dy: e.deltaY, dz: e.deltaZ, mode: e.deltaMode }));
  listen(doc, 'scroll', e => push({ ...base(e, 'scroll'), x: finite(e.target?.scrollLeft, win.scrollX), y: finite(e.target?.scrollTop, win.scrollY) }));
  listen(win, 'resize', e => push({ ...base(e, 'resize'), width: win.innerWidth, height: win.innerHeight, dpr: win.devicePixelRatio }));
  listen(win, 'blur', () => boundary('blur'), false);
  listen(win, 'pagehide', () => boundary('pagehide'), false);
  listen(doc, 'visibilitychange', () => { if (doc.hidden) boundary('hidden'); }, false);
  const api = {
    flush,
    pause() { paused = true; discard(); },
    resume() { if (!stopped && paused) { paused = false; boundary('resume'); } },
    stop() { if (stopped) return; stopped = true; generation++; flush('stop'); pressed.clear(); clearTimers(); listeners.forEach(fn => fn()); instances.delete(win); },
    getStats() { return { ...stats, buffered: records.length, paused, stopped, movementSource: source }; },
  };
  instances.set(win, api);
  return api;
}
