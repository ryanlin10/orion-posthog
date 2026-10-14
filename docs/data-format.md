# Orion events (input schema 1.4)

This is a custom analytics-event format, not a PostHog snapshot/rrweb format.

Orion sends three PostHog events: `orion_session_start` once per page load, `orion_input` with input records, and `orion_label` with live labels (see the end of this page). Earlier releases named the input event `behavioral_burst`; the dataset builder reads both.

`posthog.capture('orion_input', properties)` submits one chunk. A long burst produces multiple chunks; a burst ends after the configured idle gap. The outer PostHog event timestamp is submission time. Use the timestamps below for behavior analysis.

| Property | Type | Meaning |
| --- | --- | --- |
| `schema_version` | string | `1.4` (1.1 added `clock`; 1.2 quality, press pairing and columns; 1.3 focus records, key classes and the Orion event name; 1.4 field fills, returns to the page, scroll extents, press boxes and the page height) |
| `recorder_id` | UUID string | One running recorder instance in one document |
| `burst_id` | string | Recorder ID plus monotonic burst number |
| `batch_sequence` | integer | Chunk sequence across the recorder lifetime, starting at zero |
| `chunk_index` | integer | Chunk sequence within its burst, starting at zero |
| `reason` | string | idle, interval, limit, manual, blur, hidden, pagehide, stop, resume, or caller-provided flush reason |
| `time_origin_ms` | number | Browser performance time origin in epoch milliseconds |
| `start_ms`, `end_ms` | number | Min/max relative sample timestamps in this chunk |
| `clock` | object | `epoch_ms` (`Date.now()`) and `perf_ms` (`performance.now()`), read together at chunk flush |
| `$session_id` | string | Set by the PostHog SDK at submission (it overwrites any value the recorder supplies). Buffered input is discarded if the session or person changed before submission, so all records in a chunk belong to this session |
| `viewport` | object | Width, height, device-pixel ratio, page scroll position and `doc_height` (the page's scroll height) **at chunk flush** |
| `capture` | object | Selected movement source, coalescing capability, keyboard mask (`keyboard`), target Hz (120), synthetic-event setting |
| `quality` | object | `record_count`, cumulative `dropped_before_batch`, `excluded_events_total`, `sdk_refused_batches` at flush |
| `record_columns` | object | Samples in capture order, columnar (Orion's default) |
| `records` | array | The same samples as rows, with `recordFormat: 'rows'` |

All samples have `kind` and `t`; `t` is milliseconds relative to `time_origin_ms`, retaining the precision provided by the browser. `time_origin_ms + t` is absolute time on the browser's monotonic clock, which can drift from wall-clock time (some platforms pause it while the computer sleeps). To align with session replay or other `Date.now()` timestamps, use the chunk's anchor: wall time ≈ `clock.epoch_ms - (clock.perf_ms - t)`. This does not assert hardware-microsecond accuracy. Coalesced event timestamps may precede other recently received event kinds. Preserve source order and stable-sort by time when the analysis requires it.

Ordinary input samples also have `trusted` and `target`. Target is a recorder-local opaque integer assigned to an element; zero means unavailable. It is **not an rrweb node ID**. There is no DOM text/selector lookup table. Boundary records have no target.

## Keyboard

`kind`: `keydown` or `keyup`.

Fields: `repeat`, `composing`, and `press_id` in every mask; `key_type` with the `types` mask; `code`, `location` and `modifiers` only with the `code` mask, which reveals typed text and is never the default.

`key_type` is one of `letter`, `digit`, `space`, `delete`, `enter`, `modifier`, `navigation` or `other` (punctuation and everything else).

`press_id` is a recorder-local integer unique to each observed non-repeat keydown. Repeats and the matching keyup share it. It does not identify the same physical key across separate presses. Unknown releases omit it; exclusions and continuity gaps clear held presses. This enables overlapping-key hold durations in timing mode without exporting codes or modifier identities.

`modifiers` bitmask: Shift=1, Control=2, Alt=4, Meta=8. `code` is a physical DOM key code; never `event.key`. Missing codes remain empty. Key-up and key-down are independent observations; missing releases must remain unknown. Composition flags do not recover IME-composed characters. No clipboard or beforeinput/input text is captured (`input` records below say only that a field changed and how).

Deletes are visible only through `key_type` (`types` mask) or `code`, so the correction rate needs `mask: 'types'`. With the default timing mask it is missing, never zero.

## Pointer

`kind`: `pointermove`, `pointerdown`, `pointerup`, `pointercancel`, `click`, `dblclick`, `contextmenu`.

Fields: `pointer_id`, `pointer_type`, `x`, `y`, `page_x`, `page_y`, `dx`, `dy`, `button`, `buttons`, `pressure`, `tilt_x`, `tilt_y`, `twist`, `modifiers`. Some properties can be absent on browser-generated MouseEvent clicks. Zero values can be browser defaults, not sensor measurements.

Touch and pen samples also have `contact_w` and `contact_h`: the contact's width and height in CSS pixels (a mouse reports 1 × 1, so it has neither).

`pointerdown` also has `box_x`, `box_y`, `box_w` and `box_h` when the press lands in a control (a link, button, form field, label, summary, or an element with a button-like role): that control's bounding box in viewport CSS pixels, rounded, at the moment of the press. Overshoot (how far the pointer went past the control before coming back to press it) is measured against this box. Presses elsewhere have no box.

`x/y`: viewport CSS pixels; `page_x/page_y`: document CSS pixels. `dx/dy`: browser movementX/Y, whose device/unit behavior varies; derive positional deltas from x/y when appropriate. Preserve zoom, viewport, scrolling, and pointer type when interpreting movement. Browser pointer capture may keep targets fixed during a drag.

Movement also includes `coalesced` indicating whether the record came from a nonempty coalesced list. If a list exists, its samples replace the parent event. Otherwise the parent is recorded once. The recorder uses one movement source at a time, never combines pointerrawupdate and pointermove, and never uses predicted events. No interval polling, downsampling, or interpolation is performed.

## Other records

- `wheel`: `dx/dy/dz` and `mode` copied from deltaX/Y/Z and deltaMode. Units depend on mode (pixels, lines, pages).
- `scroll`: target scroll `x/y`; document events use window scroll coordinates. When the scroller is the page, or an area at least half the window tall, `content_height` (its scroll height) and `view_height` (its visible height) give the extent, so depth is `(y + view_height) / content_height`.
- `input`: a field changed without typing. `field` is the coarse kind as for focus records; `via` is `paste` (clipboard), `drop` (drag and drop) or `fill` (autofill, or a picked suggestion). Never the value, its length or the text. Typing, deleting and undo produce no `input` record, so these records reveal nothing the keyboard mask hides. `fills: false` turns them off.
- `visible`: the page became visible again after being hidden. `t` is when it came back and `hidden_ms` how long it was hidden. Like a boundary with nothing buffered, it waits for the next permitted input rather than sending an event on its own, and it is placed after any boundary that waited with it.
- `resize`: current `width/height/dpr`.
- `focusin`, `focusout`: `field` is a coarse kind (`text`, `number`, `textarea`, `editable`, `select`, `choice`, `button`, `link` or `other`). No names, IDs, selectors or values. Focus on an excluded element is not recorded; it pauses capture.
- `boundary`: `reason` is blur, hidden, pagehide, identity, resume, excluded, budget, or capture-gap. Blur, hidden and pagehide mean the person left (nothing was lost); recipes with the `visibility` family keep windows that contain them. A held input might never deliver its release after this boundary. Consumers should treat any boundary reason, including future values, as a break in continuity.

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
import { decodeRecords } from '@paradigmmachines/orion/codec';
const records = decodeRecords(batch); // accepts rows or columns
```

The analyzer and Python training builder accept both formats. Row batches remain the default for existing consumers. Column overhead can outweigh savings for tiny or highly heterogeneous batches.

Quality counters describe recorder-local observations before the current capture attempt. SDK acceptance is not a server acknowledgement. Subsequent batches can reveal earlier failures; missing tails remain unknown. Exclusion counts count filtered dispatched events, not necessarily all underlying coalesced samples. See the [training guide](training.md) for gap handling and annotation alignment.

## orion_session_start

`orion_version`, `schema_version`, `mask`, `pointer_source`, `models` (the model URLs or names given to `connect`), and `mood`, `base` and `self_report` (whether the built-in mood reading, the base model and the question are on).

## orion_label

Sent when a live label changes (after it wins `confirm` consecutive scores, two by default) and every 60 seconds while it holds. The SDK adds `$session_id`, `$pathname` and identity as for any event.

| Property | Meaning |
| --- | --- |
| `model`, `model_version` | The deployed model's name and version |
| `target` | The recipe target, e.g. `sentiment` |
| `label` | The class, or the score for `score(min, max)` targets |
| `confidence`, `probabilities` | The model's probability for the label and for every class (null for scores) |
| `evidence` | Up to two plain-words comparisons of this window with the training data, e.g. "time between clicks lower than usual (250 ms vs 1,500 ms)" |
| `since` | When the current label started (ISO time) |
| `earlier` | Earlier labels in this session, oldest first |
| `agents` | Agents the model was deployed for (`orion deploy --context`) |
| `window_ms` | Length of the scored window |
| `changed` | true when the label changed, false for the heartbeat |

Live scoring follows the training rules: a window is scored only if it has at least `min_events` input records and, unless the recipe allowed gaps, no capture gap. Without a score the label stays as it was; consent loss, pause and session or person changes clear it.

The built-in mood reading (`mood: true`) sends the same event with `model: "orion-mood"`, `model_version: 1`, `target: "mood"` and a label from `angry`, `frustrated`, `calm`, `anxious`, `confused` and `pleased`. It updates every 100 ms from the last two seconds of pointer movement and four seconds of presses; a new mood is sent once it has held for 2 seconds, and the heartbeat repeats it only while there is input. Its `evidence` gives pointer speed, turns per second and clicks per second.

The base model (`base: true`) sends it with `model: "orion-base"`, `model_version: 1`, `target: "affect"` and `neutral` or `negative`, scored like a deployed model on the last 10 seconds. `confidence` is the calibrated probability of the label; `negative` starts at 0.25. It is weak (right about a quarter of the time when it says `negative`); see [base-model.md](base-model.md).

## orion_self_report

Sent when someone answers or closes the question (`selfReport`). The SDK adds `$session_id`, `$pathname` and identity.

| Property | Meaning |
| --- | --- |
| `answer` | The chosen answer, or null when the question was closed |
| `dismissed` | true when closed without an answer |
| `question` | The question shown |
| `options` | The four answers in the order shown (shuffled) |
| `positive`, `negative` | The two positive and two negative answers, most relevant first |
| `ranked_by` | `survey` (a file from `orion survey`), `custom` (your `rank` function), `mood` or `default` |
| `trigger` | `time`, `mood` or `manual` |
| `recorder_id` | The recorder whose input the answer labels, as in `orion_input` |
| `shown_t`, `answered_t` | Recorder times (the same clock as input records) when the question appeared and was answered |
| `mood`, `mood_probabilities` | The built-in mood reading when the question appeared |

The self-report labeller labels `[shown_t - window length, shown_t)`: the input before the question appeared. Input on the question itself is never captured (its host element carries `data-ph-behavior-ignore`).
