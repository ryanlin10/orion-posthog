export interface RecordColumns {
  length: number;
  fields: Record<string, unknown[]>;
  present: Record<string, number[]>;
}
/** Encodes JSON records, preserving absent fields separately from null. */
export function encodeRecords(records: Record<string, unknown>[]): RecordColumns;
export function decodeRecords(batch: { records?: Record<string, unknown>[]; record_columns?: RecordColumns }): Record<string, unknown>[];
