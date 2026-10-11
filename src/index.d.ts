export const SCHEMA_VERSION: '1.3';
export const EVENT_NAME: 'orion_input';
export const SESSION_EVENT: 'orion_session_start';
export const LABEL_EVENT: 'orion_label';
export const VERSION: string;
export type Mask = 'timing' | 'types' | 'off' | 'code';

export interface PostHogClient {
  /** posthog-js returns undefined when it refuses an event. */
  capture(name: string, properties?: Record<string, unknown>, options?: { skip_client_rate_limiting?: boolean; _batchKey?: string; transport?: 'sendBeacon'; send_instantly?: boolean }): unknown;
  /** Present on the loaded posthog-js SDK; enables refusal detection. */
  __loaded?: boolean;
  has_opted_out_capturing?(): boolean;
  get_session_id?(): string;
  get_distinct_id?(): string;
  get_session_replay_url?(options?: { withTimestamp?: boolean; timestampLookBack?: number }): string;
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
  /** 'timing' (default): press/release times only. 'types': adds a key class. 'code': physical key codes (reveals text). */
  mask?: Mask;
  /** Older name for `mask`. */
  keyboard?: Mask;
  /** Record focus changes with a coarse field kind (default true). */
  focus?: boolean;
  movementSource?: 'auto' | 'pointermove' | 'pointerrawupdate';
  excludeSelector?: string;
  isAllowed?: () => boolean;
  /** For controlled tests only; synthetic DOM events are excluded by default. */
  allowSynthetic?: boolean;
}
export interface RecorderStats {
  recorded: number; submitted: number; batches: number; dropped: number;
  excluded: number; errors: number; buffered: number; paused: boolean; stopped: boolean; movementSource: string; mask: Mask;
  refusedBatches: number; budgetDropped: number; attemptedBytes: number;
}
export interface Recorder {
  flush(reason?: string): void;
  pause(): void;
  resume(): void;
  stop(): void;
  getStats(): RecorderStats;
  permitted(): boolean;
}
export function createRecorder(posthog: PostHogClient, options?: RecorderOptions & { onRecord?: (row: Record<string, unknown>) => void; onReset?: () => void }): Recorder;

/** A model file written by `orion train` / `orion deploy` (format "orion-model", version 1). */
export interface OrionModel {
  format: 'orion-model';
  format_version: 1;
  name: string;
  version: number;
  target: { name: string; type: 'categorical' | 'continuous'; classes?: string[]; min?: number; max?: number; binary?: boolean };
  window: { size_ms: number; align: 'past' | 'symmetric' | 'future'; min_events?: number; max_events?: number; allow_gaps?: boolean };
  estimator: { kind: 'linear' | 'forest' | 'boosted' | 'onnx'; [key: string]: unknown };
  agents?: string[];
  [key: string]: unknown;
}

export interface OrionContext {
  model: string;
  version: number;
  target: string;
  label: string | number;
  confidence: number | null;
  probabilities: Record<string, number> | null;
  since: string;
  earlier: Array<string | number>;
  evidence: string[];
  agents: string[];
  session_id: string | null;
  replay_url: string | null;
  updated_at: string;
}

export interface ConnectOptions extends RecorderOptions {
  /** Model URLs (from `orion deploy`) or model objects to label sessions with. */
  models?: Array<string | OrionModel>;
  live?: { every?: number; heartbeat?: number; confirm?: number; send?: boolean };
  /** onnxruntime-web, for sequence models (defaults to window.ort). */
  ort?: unknown;
}

export interface Orion {
  version: string;
  /** Names of the models that loaded. */
  ready: Promise<string[]>;
  load(model: string | OrionModel): Promise<string>;
  context(model?: string): OrionContext | null;
  contextText(model?: string): string;
  labels(): OrionContext[];
  onLabel(fn: (context: OrionContext) => void): () => void;
  flush(reason?: string): void;
  pause(): void;
  resume(): void;
  stop(): void;
  getStats(): RecorderStats & { live: { ticks: number; scored: number; noRead: number; labelsSent: number; errors: number; models: string[]; buffered: number; noReadReasons: Record<string, string | null> } };
}

export function connect(posthog: PostHogClient, options?: ConnectOptions): Orion;
export const orion: { connect: typeof connect; version: string };
