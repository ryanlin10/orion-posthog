export const SCHEMA_VERSION: '1.0';
export const EVENT_NAME: 'behavioral_burst';
export interface PostHogClient {
  capture(name: string, properties?: Record<string, unknown>, options?: { transport: 'sendBeacon'; send_instantly: boolean }): unknown;
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
}
export interface Recorder {
  flush(reason?: string): void;
  pause(): void;
  resume(): void;
  stop(): void;
  getStats(): RecorderStats;
}
export function createRecorder(posthog: PostHogClient, options?: RecorderOptions): Recorder;
