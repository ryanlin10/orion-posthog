export const SCHEMA_VERSION: '1.2';
export const EVENT_NAME: 'behavioral_burst';
export interface PostHogClient {
  /** posthog-js returns undefined when it refuses an event. */
  capture(name: string, properties?: Record<string, unknown>, options?: { skip_client_rate_limiting?: boolean; _batchKey?: string; transport?: 'sendBeacon'; send_instantly?: boolean }): unknown;
  /** Present on the loaded posthog-js SDK; enables refusal detection. */
  __loaded?: boolean;
  has_opted_out_capturing?(): boolean;
  get_session_id?(): string;
  get_distinct_id?(): string;
}
export interface RecorderOptions {
  window?: Window;
  idleMs?: number;
  flushMs?: number;
  maxEvents?: number;
  maxBytes?: number;
  recordFormat?: 'rows' | 'columns';
  /** Recorder-properties byte budgets; 0 disables the limit. SDK enrichment is not included. */
  maxBytesPerMinute?: number;
  maxTotalBytes?: number;
  keyboard?: 'code' | 'timing' | 'off';
  movementSource?: 'auto' | 'pointermove' | 'pointerrawupdate';
  excludeSelector?: string;
  isAllowed?: () => boolean;
  /** For controlled tests only; synthetic DOM events are excluded by default. */
  allowSynthetic?: boolean;
}
export interface RecorderStats {
  recorded: number; submitted: number; batches: number; dropped: number;
  excluded: number; errors: number; buffered: number; paused: boolean; stopped: boolean; movementSource: string;
  refusedBatches: number; budgetDropped: number; attemptedBytes: number;
}
export interface Recorder {
  flush(reason?: string): void;
  pause(): void;
  resume(): void;
  stop(): void;
  getStats(): RecorderStats;
}
export function createRecorder(posthog: PostHogClient, options?: RecorderOptions): Recorder;
