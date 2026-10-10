/** Offline analysis of decoded behavioral_burst properties; never an emotion classifier. */
export function analyzeBatches(batches) {
  // Delivery order can differ from capture order. A recorder UUID owns one sequence.
  const streams = new Map();
  let duplicateBatches = 0, missingBatches = 0;
  for (const batch of batches) {
    if (!Number.isInteger(batch.batch_sequence) || batch.batch_sequence < 0 || !batch.recorder_id) {
      throw new TypeError('Expected recorder_id and a nonnegative integer batch_sequence');
    }
    if (!streams.has(batch.recorder_id)) streams.set(batch.recorder_id, new Map());
    const stream = streams.get(batch.recorder_id);
    if (stream.has(batch.batch_sequence)) { duplicateBatches++; continue; }
    stream.set(batch.batch_sequence, batch);
  }
  const counts = {}, holds = [], intervals = [];
  let records = 0, unmatchedUps = 0, openKeyDowns = 0;
  for (const stream of streams.values()) {
    const held = new Map(), lastMoves = new Map();
    let previousSequence, previousOrigin;
    const reset = () => { held.clear(); lastMoves.clear(); };
    for (const [sequence, batch] of [...stream].sort(([a], [b]) => a - b)) {
      if (previousSequence !== undefined && sequence !== previousSequence + 1) {
        missingBatches += sequence - previousSequence - 1;
        reset();
      }
      if (previousOrigin !== undefined && batch.time_origin_ms !== previousOrigin) reset();
      previousSequence = sequence; previousOrigin = batch.time_origin_ms;
      for (const row of batch.records) {
        records++;
        counts[row.kind] = (counts[row.kind] ?? 0) + 1;
        if (row.kind === 'boundary') reset();
        const key = `${row.code}:${row.location}`;
        if (row.kind === 'keydown' && row.code && !row.repeat) held.set(key, row.t);
        if (row.kind === 'keyup' && row.code) {
          const start = held.get(key);
          if (start !== undefined && row.t >= start) holds.push(row.t - start);
          else unmatchedUps++;
          held.delete(key);
        }
        if (row.kind === 'pointermove') {
          const previous = lastMoves.get(row.pointer_id), delta = row.t - previous;
          if (previous !== undefined && delta > 0 && delta < 100) intervals.push(delta);
          lastMoves.set(row.pointer_id, row.t);
        }
      }
    }
    openKeyDowns += held.size;
  }
  intervals.sort((a, b) => a - b);
  const middle = Math.floor(intervals.length / 2);
  const median = intervals.length ? (intervals.length % 2 ? intervals[middle] : (intervals[middle - 1] + intervals[middle]) / 2) : null;
  return { records, counts, keyHoldMs: holds, unmatchedKeyUps: unmatchedUps, openKeyDowns, duplicateBatches, missingBatches,
    movement: { measuredIntervals: intervals.length, medianIntervalMs: median, medianHz: median ? 1000 / median : null,
      fractionAtLeast120Hz: intervals.length ? intervals.filter(x => x <= 1000 / 120 + 0.05).length / intervals.length : null } };
}
