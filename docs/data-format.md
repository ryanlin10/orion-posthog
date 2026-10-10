# behavioral_burst v1.0

This is a custom analytics-event format, not a PostHog snapshot/rrweb format.

`posthog.capture('behavioral_burst', properties)` submits one chunk. A long burst produces multiple chunks; a burst ends after the configured idle gap. The outer PostHog event timestamp is submission time. Use the timestamps below for behavior analysis.

| Property | Type | Meaning |
| --- | --- | --- |
| `schema_version` | string | `1.0` |
| `recorder_id` | UUID string | One running recorder instance in one document |
| `burst_id` | string | Recorder ID plus monotonic burst number |
| `batch_sequence` | integer | Chunk sequence across the recorder lifetime, starting at zero |
| `chunk_index` | integer | Chunk sequence within its burst, starting at zero |
| `reason` | string | idle, interval, limit, manual, blur, hidden, pagehide, stop, resume, or caller-provided flush reason |
| `time_origin_ms` | number | Browser performance time origin in epoch milliseconds |
| `start_ms`, `end_ms` | number | Min/max relative sample timestamps in this chunk |
| `$session_id` | string/null | SDK session at collection time; SDK supplies its other normal identity properties |
| `viewport` | object | Width, height, device-pixel ratio, and page scroll position **at chunk flush** |
| `capture` | object | Selected movement source, coalescing capability, keyboard mode, target Hz (120), synthetic-event setting |
| `records` | array | Actual samples in capture order |

All samples have `kind` and `t`; `t` is milliseconds relative to `time_origin_ms`, retaining the precision provided by the browser. Absolute time is `time_origin_ms + t`. This does not assert hardware-microsecond accuracy. Coalesced event timestamps may precede other recently received event kinds. Preserve source order and stable-sort by time when the analysis requires it.

Ordinary input samples also have `trusted` and `target`. Target is a recorder-local opaque integer assigned to an element; zero means unavailable. It is **not an rrweb node ID**. There is no DOM text/selector lookup table. Boundary records have no target.

## Keyboard

`kind`: `keydown` or `keyup`.

Fields: `code` and `location` (code mode only), `repeat`, `composing`, `modifiers`.

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
- `boundary`: `reason` is blur, hidden, pagehide, identity, or resume. A held input might never deliver its release after this boundary.

Session/person changes discard buffered old-identity input, then insert an identity boundary before new input. Pause discards pending data; resume inserts a boundary. Errors/dropped counts are available locally through `getStats()`; they are not proof that PostHog ingested all other records.

## Integrity checks

1. Group by recorder ID; deduplicate by batch sequence before analysis.
2. Preserve all chunks and source timestamps, including negative/zero relative times if supplied by the browser.
3. Treat sequence gaps, lifecycle boundaries, and missing down/up pairs as incomplete evidence.
4. Correlate via `$session_id`; do not label these events native rage clicks or native replay snapshots.
5. Store the complete `capture` metadata when exporting; a 120 target does not establish an achieved 120 Hz.
