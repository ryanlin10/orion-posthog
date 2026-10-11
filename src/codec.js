// Columnar JSON retains every JSON value and distinguishes absent fields from null.
export function encodeRecords(records) {
  const fields = Object.create(null), present = Object.create(null);
  for (const key of new Set(records.flatMap(Object.keys))) {
    const indices = [];
    fields[key] = records.map((row, i) => {
      if (Object.hasOwn(row, key) && row[key] !== undefined) { indices.push(i); return row[key]; }
      return null;
    });
    if (indices.length !== records.length) present[key] = indices;
  }
  return { length: records.length, fields, present };
}

export function decodeRecords(batch) {
  if (Array.isArray(batch.records)) return batch.records;
  const c = batch.record_columns;
  if (!c || !Number.isInteger(c.length) || c.length < 0 || c.length > 10000 || !c.fields || !c.present) throw new TypeError('Invalid record columns');
  const rows = Array.from({ length: c.length }, () => ({}));
  for (const [key, values] of Object.entries(c.fields)) {
    if (!Array.isArray(values) || values.length !== c.length) throw new TypeError('Invalid column length');
    const indices = Object.hasOwn(c.present, key) ? c.present[key] : values.map((_, i) => i);
    if (!Array.isArray(indices) || new Set(indices).size !== indices.length) throw new TypeError('Invalid column presence');
    for (const i of indices) {
      if (!Number.isInteger(i) || i < 0 || i >= c.length) throw new TypeError('Invalid column index');
      Object.defineProperty(rows[i], key, { value: values[i], enumerable: true, configurable: true, writable: true });
    }
  }
  return rows;
}
