import test from 'node:test';
import assert from 'node:assert/strict';
import { createRecorder } from '../src/index.js';
import { analyzeBatches } from '../src/analysis.js';
import { encodeRecords, decodeRecords } from '../src/codec.js';
import { environment } from './helpers.js';

test('stop/pause from a capture hook aborts the rest of a coalesced packet', () => {
  for (const action of ['stop', 'pause']) {
    const e = environment(); let r, count = 0;
    e.posthog.capture = () => { count++; r[action](); };
    r = createRecorder(e.posthog, { window: e.win, maxEvents: 1, allowSynthetic: true });
    e.emit('pointermove', { getCoalescedEvents: () => Array.from({ length: 8 }, (_, i) => ({ timeStamp: i, clientX: i })) });
    assert.equal(count, 1); assert.equal(r.getStats().buffered, 0);
    r.stop();
  }
});
test('pause followed by resume inside a hook invalidates the current input packet', () => {
  const e = environment(); let r, calls = 0;
  e.posthog.capture = () => { calls++; r.pause(); r.resume(); };
  r = createRecorder(e.posthog, { window: e.win, maxEvents: 1, allowSynthetic: true });
  e.emit('pointermove', { getCoalescedEvents: () => Array.from({ length: 8 }, (_, i) => ({ timeStamp: i })) });
  assert.equal(calls, 1); assert.equal(r.getStats().buffered, 0); r.stop();
});
test('timing mode pairs overlapping keys using fresh IDs without key or modifier identities', () => {
  const e = environment(), r = createRecorder(e.posthog, { window: e.win, keyboard: 'timing', allowSynthetic: true });
  for (const [type, code, timeStamp] of [['keydown','KeyA',1],['keydown','KeyB',2],['keyup','KeyA',3],['keyup','KeyB',5],['keydown','KeyA',6],['keyup','KeyA',9]]) e.emit(type,{code,timeStamp,shiftKey:true});
  r.stop();
  const rows = e.calls.flatMap(c => c[1].records);
  assert.deepEqual(rows.map(r => r.press_id), [1,2,1,2,3,3]);
  assert.ok(rows.every(r => !('code' in r) && !('location' in r) && !('modifiers' in r)));
  assert.deepEqual(analyzeBatches(e.calls.map(c=>c[1])).keyHoldMs, [2,3,3]);
});
test('budgets are bounded and report missing evidence after replenishing', () => {
  const e = environment(); let now = 0; e.win.performance.now = () => now;
  const r = createRecorder(e.posthog, { window:e.win, maxBytesPerMinute:2000, allowSynthetic:true });
  for (let i=0;i<6;i++) {e.emit('pointermove',{timeStamp:i});r.flush();}
  assert.ok(r.getStats().budgetDropped > 0);
  assert.ok(r.getStats().attemptedBytes <= 2000);
  now=60001; e.emit('pointermove',{timeStamp:60001});r.stop();
  const batch=e.calls.at(-1)[1];
  assert.ok(batch.quality.dropped_before_batch > 0);
  assert.equal(batch.records[0].reason,'budget');
});
test('idle deadline uses monotonic time, independent of wall-clock adjustments', () => {
  const e=environment(); let now=0, next=0; const timers=new Map();
  e.win.performance.now=()=>now;
  e.win.setTimeout=(fn,ms)=>{timers.set(++next,{fn,ms});return next;};
  e.win.clearTimeout=id=>timers.delete(id);
  const r=createRecorder(e.posthog,{window:e.win,allowSynthetic:true});
  e.emit('keydown',{code:'KeyA'});
  now=500;
  const dateNow=Date.now; Date.now=()=>-3600000;
  try { [...timers.values()].find(t=>t.ms===500).fn(); }
  finally { Date.now=dateNow; r.stop(); }
  assert.equal(e.calls.length,1); assert.equal(e.calls[0][1].reason,'idle');
});
test('column encoding round-trips sparse values, nulls, Unicode and precision', () => {
  const records=[{kind:'keydown',t:0.123456789,code:'KeyA',extra:null},{kind:'boundary',t:2,reason:'😀\ud800é'}, {kind:'pointermove',t:3,x:0,y:-0.2}];
  assert.deepEqual(decodeRecords(JSON.parse(JSON.stringify({record_columns:encodeRecords(records)}))),records);
  assert.throws(()=>decodeRecords({record_columns:{length:2,fields:{t:[1]},present:{}}}),/length/);
});
test('column format works with the analyzer and preserves the byte limit', () => {
  const e=environment(), r=createRecorder(e.posthog,{window:e.win,allowSynthetic:true,recordFormat:'columns'});
  for(let i=0;i<200;i++)e.emit('pointermove',{timeStamp:i,clientX:i,pointerId:1});r.stop();
  assert.equal(analyzeBatches(e.calls.map(c=>c[1])).records,200);
  assert.ok(e.calls.every(c=>Buffer.byteLength(JSON.stringify(c[1]))<=48000));
});
