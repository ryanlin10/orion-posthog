export const SCHEMA_VERSION = '1.0';
export const EVENT_NAME = 'behavioral_burst';
const PRIVATE = 'input[type="password"],[data-ph-behavior-ignore],[data-private],.ph-no-capture,.ph-sensitive';
const instances = new WeakMap();
const bytes = value => new TextEncoder().encode(JSON.stringify(value)).length;
const finite = (n, fallback = 0) => Number.isFinite(n) ? n : fallback;

/** Capture actual browser samples only. No polling, interpolation or predicted events. */
export function createRecorder(posthog, options = {}) {
  const win = options.window ?? globalThis.window;
  if (!win?.document) throw new Error('createRecorder requires a browser window');
  if (typeof posthog?.capture !== 'function') throw new TypeError('A loaded PostHog SDK is required');
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
  };
  for (const [key, min, max] of [['idleMs', 50, 60000], ['flushMs', 50, 60000], ['maxEvents', 1, 10000], ['maxBytes', 4096, 48000]]) {
    if (!Number.isInteger(config[key]) || config[key] < min || config[key] > max) throw new RangeError(`Invalid ${key}`);
  }
  if (!['code', 'timing', 'off'].includes(config.keyboard)) throw new RangeError('Invalid keyboard mode');
  if (!['auto', 'pointermove', 'pointerrawupdate'].includes(config.movementSource)) throw new RangeError('Invalid movementSource');
  const excluded = options.excludeSelector ? `${PRIVATE},${options.excludeSelector}` : PRIVATE;
  doc.querySelector(excluded); // Validate before attaching any listeners.
  const origin = win.performance.timeOrigin;
  const id = win.crypto.randomUUID();
  const raw = 'onpointerrawupdate' in win;
  const source = config.movementSource === 'auto' ? (raw ? 'pointerrawupdate' : 'pointermove') : config.movementSource;
  const stats = { recorded: 0, submitted: 0, batches: 0, dropped: 0, excluded: 0, errors: 0 };
  const listeners = [];
  const targets = new WeakMap();
  let nextTarget = 1, records = [], recordBytes = 0, identity = null;
  let burst = 0, chunk = 0, sequence = 0, lastT = null, idleTimer, flushTimer;
  let stopped = false, paused = false;

  function permitted() {
    try { return !paused && config.isAllowed() && posthog.has_opted_out_capturing?.() !== true; }
    catch { return false; }
  }
  function currentIdentity() {
    return [posthog.get_session_id?.() ?? null, posthog.get_distinct_id?.() ?? null];
  }
  function clearTimers() {
    win.clearTimeout(idleTimer); win.clearTimeout(flushTimer);
    idleTimer = flushTimer = undefined;
  }
  function discard() {
    stats.dropped += records.length;
    records = []; recordBytes = 0; lastT = null; chunk = 0;
    clearTimers();
  }
  function flush(reason = 'manual') {
    if (!records.length) return;
    if (!permitted() || JSON.stringify(currentIdentity()) !== JSON.stringify(identity)) { discard(); return; }
    const batch = {
      schema_version: SCHEMA_VERSION, recorder_id: id, burst_id: `${id}:${burst}`,
      chunk_index: chunk++, batch_sequence: sequence++, reason,
      time_origin_ms: origin, start_ms: Math.min(...records.map(e => e.t)), end_ms: Math.max(...records.map(e => e.t)),
      $session_id: identity[0],
      viewport: { width: win.innerWidth, height: win.innerHeight, dpr: win.devicePixelRatio, scroll_x: win.scrollX, scroll_y: win.scrollY },
      capture: { movement_source: source, coalesced_supported: typeof win.PointerEvent?.prototype.getCoalescedEvents === 'function', keyboard: config.keyboard, target_hz: 120, synthetic_allowed: config.allowSynthetic },
      records,
    };
    const count = records.length;
    records = []; recordBytes = 0;
    win.clearTimeout(flushTimer); flushTimer = undefined;
    try {
      if (bytes(batch) > config.maxBytes) throw new Error('Batch exceeds configured byte limit');
      // Calling capture hands delivery/retries to the existing SDK; it is not a server ACK.
      posthog.capture(EVENT_NAME, batch, reason === 'hidden' || reason === 'pagehide' ? { transport: 'sendBeacon', send_instantly: true } : undefined);
      stats.submitted += count; stats.batches++;
    } catch { stats.dropped += count; stats.errors++; }
  }
  function push(record) {
    if (stopped) return;
    if (!permitted()) { discard(); return; }
    const nextIdentity = currentIdentity();
    const changed = identity && JSON.stringify(identity) !== JSON.stringify(nextIdentity);
    if (changed) discard();
    identity = nextIdentity;
    if (changed) push({ kind: 'boundary', t: record.t, reason: 'identity' });
    if (lastT === null || record.t - lastT > config.idleMs) {
      flush('idle'); burst++; chunk = 0;
    }
    lastT = record.t;
    const size = bytes(record) + 1;
    if (size > config.maxBytes - 2048) { stats.dropped++; return; }
    if (records.length >= config.maxEvents || recordBytes + size > config.maxBytes - 2048) flush('limit');
    records.push(record); recordBytes += size; stats.recorded++;
    if (flushTimer === undefined) flushTimer = win.setTimeout(() => flush('interval'), config.flushMs);
    win.clearTimeout(idleTimer);
    idleTimer = win.setTimeout(() => { flush('idle'); lastT = null; chunk = 0; }, config.idleMs);
  }
  function timestamp(e) {
    // Older implementations may expose epoch rather than performance-relative timestamps.
    const t = finite(e.timeStamp, win.performance.now());
    return t > 1e12 ? t - origin : t;
  }
  function privateTarget(e) {
    const path = e.composedPath?.() ?? [e.target];
    return [...path, doc.activeElement].some(node => node?.matches?.(excluded) || node?.closest?.(excluded));
  }
  function targetId(e) {
    const node = e.composedPath?.()[0] ?? e.target;
    if (!node || typeof node !== 'object') return 0;
    if (!targets.has(node)) targets.set(node, nextTarget++);
    return targets.get(node);
  }
  function base(e, kind) {
    return { kind, t: timestamp(e), trusted: e.isTrusted === true, target: targetId(e) };
  }
  const modifiers = e => (e.shiftKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.altKey ? 4 : 0) | (e.metaKey ? 8 : 0);
  function keyboard(e) {
    if (config.keyboard === 'off') return;
    push({ ...base(e, e.type), ...(config.keyboard === 'code' ? { code: String(e.code ?? '').slice(0, 40), location: e.location } : {}), repeat: e.repeat, composing: e.isComposing, modifiers: modifiers(e) });
  }
  function pointer(e, parent = e) {
    return { ...base(parent, parent.type === source ? 'pointermove' : parent.type), t: timestamp(e), pointer_id: e.pointerId,
      pointer_type: e.pointerType, x: finite(e.clientX), y: finite(e.clientY), page_x: finite(e.pageX), page_y: finite(e.pageY),
      dx: finite(e.movementX), dy: finite(e.movementY), button: e.button, buttons: e.buttons,
      pressure: finite(e.pressure), tilt_x: finite(e.tiltX), tilt_y: finite(e.tiltY), twist: finite(e.twist), modifiers: modifiers(parent) };
  }
  function movement(e) {
    // Use the coalesced list OR its parent. Including both duplicates the final sample.
    let samples;
    try { samples = e.getCoalescedEvents?.(); } catch { /* unsupported implementation */ }
    const list = samples?.length ? samples : [e];
    for (const sample of list) push({ ...pointer(sample, e), coalesced: !!samples?.length });
  }
  function listen(target, type, fn, filter = true) {
    const handler = e => {
      try {
        if (stopped) return;
        if (filter && (!config.allowSynthetic && !e.isTrusted)) return;
        if (filter && privateTarget(e)) { stats.excluded++; return; }
        fn(e);
      } catch { stats.errors++; }
    };
    target.addEventListener(type, handler, { capture: true, passive: true });
    listeners.push(() => target.removeEventListener(type, handler, true));
  }
  function boundary(reason) {
    if (permitted()) push({ kind: 'boundary', t: win.performance.now(), reason });
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
    stop() { if (stopped) return; flush('stop'); stopped = true; clearTimers(); listeners.forEach(fn => fn()); instances.delete(win); },
    getStats() { return { ...stats, buffered: records.length, paused, stopped, movementSource: source }; },
  };
  instances.set(win, api);
  return api;
}
