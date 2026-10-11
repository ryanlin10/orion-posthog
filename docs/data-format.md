# behavioral_burst v1.2

This is a custom analytics-event format, not a PostHog snapshot/rrweb format.

`posthog.capture('behavioral_burst', properties)` submits one chunk. A long burst produces multiple chunks; a burst ends after the configured idle gap. The outer PostHog event timestamp is submission time. Use the timestamps below for behavior analysis.

| Property | Type | Meaning |
| --- | --- | --- |
| `schema_version` | string | `1.2` (1.1 added `clock`; 1.2 adds quality, press pairing and optional columns) |
| `recorder_id` | UUID string | One running recorder instance in one document |
| `burst_id` | string | Recorder ID plus monotonic burst number |
| `batch_sequence` | integer | Chunk sequence across the recorder lifetime, starting at zero |
| `chunk_index` | integer | Chunk sequence within its burst, starting at zero |
| `reason` | string | idle, interval, limit, manual, blur, hidden, pagehide, stop, resume, or caller-provided flush reason |
| `time_origin_ms` | number | Browser performance time origin in epoch milliseconds |
| `start_ms`, `end_ms` | number | Min/max relative sample timestamps in this chunk |
| `clock` | object | `epoch_ms` (`Date.now()`) and `perf_ms` (`performance.now()`), read together at chunk flush |
| `$session_id` | string | Set by the PostHog SDK at submission (it overwrites any value the recorder supplies). Buffered input is discarded if the session or person changed before submission, so all records in a chunk belong to this session |
| `viewport` | object | Width, height, device-pixel ratio, and page scroll position **at chunk flush** |
| `capture` | object | Selected movement source, coalescing capability, keyboard mode, target Hz (120), synthetic-event setting |
| `quality` | object | `record_count`, cumulative `dropped_before_batch`, `excluded_events_total`, `sdk_refused_batches` at flush |
| `records` | array | Actual samples in capture order, default format |
| `record_columns` | object | Optional columnar representation instead of `records` |

All samples have `kind` and `t`; `t` is milliseconds relative to `time_origin_ms`, retaining the precision provided by the browser. `time_origin_ms + t` is absolute time on the browser's monotonic clock, which can drift from wall-clock time (some platforms pause it while the computer sleeps). To align with session replay or other `Date.now()` timestamps, use the chunk's anchor: wall time ≈ `clock.epoch_ms - (clock.perf_ms - t)`. This does not assert hardware-microsecond accuracy. Coalesced event timestamps may precede other recently received event kinds. Preserve source order and stable-sort by time when the analysis requires it.

Ordinary input samples also have `trusted` and `target`. Target is a recorder-local opaque integer assigned to an element; zero means unavailable. It is **not an rrweb node ID**. There is no DOM text/selector lookup table. Boundary records have no target.

## Keyboard

`kind`: `keydown` or `keyup`.

Fields: `code`, `location`, and `modifiers` (code mode only), `repeat`, `composing`, and optional `press_id`.

`press_id` is a recorder-local integer unique to each observed non-repeat keydown. Repeats and the matching keyup share it. It does not identify the same physical key across separate presses. Unknown releases omit it; exclusions and continuity gaps clear held presses. This enables overlapping-key hold durations in timing mode without exporting codes or modifier identities.

`modifiers` bitmask: Shift=1, Control=2, Alt=4, Meta=8. `code` is a physical DOM key code; never `event.key`. Missing codes remain empty. Key-up and key-down are independent observations; missing releases must remain unknown. Composition flags do not recover IME-composed characters. No clipboard or beforeinput/input text is captured.

## Pointer

`kind`: `pointermove`, `pointerdown`, `pointerup`, `pointercancel`, `click`, `dblclick`, `contextmenu`.

Fields: `pointer_id`, `pointer_type`, `x`, `y`, `page_x`, `page_y`, `dx`, `dy`, `button`, `buttons`, `pressure`, `tilt_x`, `tilt_y`, `twist`, `modifiers`. Some properties can be absent on browser-generated MouseEvent clicks. Zero values can be browser defaults, not sensor measurements.

`x/y`: viewport CSS pixels; `page_x/page_y`: document CSS pixels. `dx/dy`: browser movementX/Y, whose device/unit behavior varies; derive positional deltas from x/y when appropriate. Preserve zoom, viewport, scrolling, and pointer type when interpreting movement. Browser pointer capture may keep targets fixed during a drag.

Movement also includes `coalesced` indicating whether the record came from a nonempty coalesced list. If a list exists, its samples replace the parent event. Otherwise the parent is recorded once. The recorder uses one movement source at a time, never combines pointerrawupdate and pointermove, and never uses predicted events. No interval polling, downsampling, or interpolation is performed.

## Other records

- `wheel`: `dx/dy/dz` and `mode` copied from deltaX/Y/Z and deltaMode. Units depend on mode (pixels, lines, pages).
- `scroll`: target scroll `x/y`; document events use window scroll coordinates.
- `resize`: current `width/height/dpr`.
- `boundary`: `reason` is blur, hidden, pagehide, identity, resume, excluded, budget, or capture-gap. A held input might never deliver its release after this boundary. Consumers should treat any boundary reason, including future values, as a break in continuity.

Session/person changes discard buffered old-identity input, then insert an identity boundary before new input. Pause discards pending data; resume inserts a boundary. A lifecycle boundary (blur, hidden, pagehide, resume) that occurs while input is buffered is appended with its own timestamp and the chunk is flushed. With nothing buffered, no event is sent: the boundary is held and inserted immediately before the next permitted record, with that record's timestamp. Errors/dropped counts are available locally through `getStats()`; they are not proof that PostHog ingested all other records.

After excluded input or an observed permission/SDK failure (including a chunk the SDK refuses), a boundary is inserted immediately before the next permitted record, with that record's timestamp. This marks a gap, not the precise time or duration of the excluded activity. Nothing is sent merely because a private field is used. Focus inside open shadow roots is checked recursively; closed shadow roots require an excluded host. Applications must call `pause()` on consent revocation to notify the recorder even when no input or flush occurs during the interruption.

## Integrity checks

1. Group by recorder ID; deduplicate by batch sequence before analysis.
2. Preserve all chunks and source timestamps, including negative/zero relative times if supplied by the browser.
3. Treat sequence gaps, lifecycle boundaries, and missing down/up pairs as incomplete evidence.
4. Correlate via `$session_id`; do not label these events native rage clicks or native replay snapshots.
5. Store the complete `capture` metadata when exporting; a 120 target does not establish an achieved 120 Hz.

## Columnar format

With `recordFormat: 'columns'`, `record_columns` contains `length`, `fields`, and `present`. Every field column has `length` values. Sparse fields have a list of present row indices in `present`; missing slots use null placeholders. A field absent from `present` is present in all rows. This distinguishes an explicit null from a missing property. JSON numeric precision and capture order are preserved; this is not quantization or resampling.

```js
import { decodeRecords } from 'posthog-behavioral-recorder/codec';
const records = decodeRecords(batch); // accepts rows or columns
```

The analyzer and Python training builder accept both formats. Row batches remain the default for existing consumers. Column overhead can outweigh savings for tiny or highly heterogeneous batches.

Quality counters describe recorder-local observations before the current capture attempt. SDK acceptance is not a server acknowledgement. Subsequent batches can reveal earlier failures; missing tails remain unknown. Exclusion counts count filtered dispatched events, not necessarily all underlying coalesced samples. See the [training guide](training.md) for gap handling and annotation alignment.
