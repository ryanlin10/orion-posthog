/** Offline analysis of decoded behavioral_burst properties; never an emotion classifier. */
export function analyzeBatches(batches) {
  const rows = batches.flatMap(b => b.records.map(r => ({ ...r, recorder: b.recorder_id, at: b.time_origin_ms + r.t })))
    .sort((a, b) => a.at - b.at);
  const counts = {}, held = new Map(), holds = [], intervals = [];
  const lastMoves = new Map();
  let unmatchedUps = 0;
  for (const row of rows) {
    counts[row.kind] = (counts[row.kind] ?? 0) + 1;
    if (row.kind === 'boundary') {
      for (const key of held.keys()) if (key.startsWith(`${row.recorder}:`)) held.delete(key);
      for (const key of lastMoves.keys()) if (key.startsWith(`${row.recorder}:`)) lastMoves.delete(key);
    }
    if (row.kind === 'keydown' && row.code && !row.repeat) held.set(`${row.recorder}:${row.code}:${row.location}`, row.at);
    if (row.kind === 'keyup' && row.code) {
      const key = `${row.recorder}:${row.code}:${row.location}`, start = held.get(key);
      if (start !== undefined) { holds.push(row.at - start); held.delete(key); } else unmatchedUps++;
    }
    if (row.kind === 'pointermove') {
      const key = `${row.recorder}:${row.pointer_id}`, previous = lastMoves.get(key);
      const delta = row.at - previous;
      if (previous !== undefined && delta > 0 && delta < 100) intervals.push(delta);
      lastMoves.set(key, row.at);
    }
  }
  intervals.sort((a, b) => a - b);
  const median = intervals.length ? intervals[Math.floor(intervals.length / 2)] : null;
  return { records: rows.length, counts, keyHoldMs: holds, unmatchedKeyUps: unmatchedUps, openKeyDowns: held.size,
    movement: { measuredIntervals: intervals.length, medianIntervalMs: median, medianHz: median ? 1000 / median : null,
      fractionAtLeast120Hz: intervals.length ? intervals.filter(x => x <= 1000 / 120 + 0.05).length / intervals.length : null } };
}
