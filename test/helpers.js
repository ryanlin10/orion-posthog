import { webcrypto } from 'node:crypto';
// The recorder mechanics tests were written against the pre-Orion defaults; Orion's defaults have their own tests.
export const LEGACY = { keyboard: 'code', recordFormat: 'rows', idleMs: 500, flushMs: 1000, maxEvents: 128, focus: false };
export function environment() {
  const win = new EventTarget();
  const doc = new EventTarget();
  Object.assign(doc, { querySelector: () => null, activeElement: null, hidden: false });
  Object.assign(win, { document: doc, performance: { timeOrigin: 1700000000000, now: () => performance.now() }, crypto: webcrypto,
    innerWidth: 1440, innerHeight: 900, devicePixelRatio: 2, scrollX: 0, scrollY: 0,
    setTimeout, clearTimeout, setInterval, clearInterval, PointerEvent: class {} });
  const calls = [];
  let optedOut = false, session = 'session-a', person = 'person-a';
  const posthog = { capture: (...args) => calls.push(structuredClone(args)), get_session_id: () => session,
    get_distinct_id: () => person, has_opted_out_capturing: () => optedOut };
  const target = { matches: () => false, closest: () => null };
  function emit(type, props = {}, on = doc) {
    const event = new Event(type);
    Object.defineProperties(event, Object.fromEntries(Object.entries({ target, timeStamp: 0, ...props }).map(([key, value]) => [key, { value }])));
    on.dispatchEvent(event);
  }
  return { win, doc, posthog, calls, emit, target, optOut: v => optedOut = v, identity: (s, p) => { session = s; person = p; } };
}
