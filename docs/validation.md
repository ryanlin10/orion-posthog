# Validation — 2026-10-11

## v0.2.0 verification

- 39 JavaScript unit tests cover capture, lifecycle callback interruption, monotonic idle timing, timing-only press pairing, loss/budget accounting and lossless column decoding.
- 29 standard-library Python tests cover label alignment, configurable targets, custom feature hooks, deterministic grouped splits, gap spans, conflicting duplicates, provenance, malformed data, and bounded window expansion.
- 3 optional reference-trainer tests verify train-only imputation/scaling, holdout evaluation opt-in, checksum enforcement and split-group isolation.
- Browser integration passes with posthog-js **1.280.0 and 1.438.7**, including delayed HTML-snippet initialization, both network-envelope formats, exact input payloads and session identity, client rate-limit isolation, and **20 chunks in one SDK transport request**.
- Synthetic dispatch stress retains all 91,200 samples across the 120/1,000/8,000 Hz timestamp fixtures. The private CrowdCast fixture retains all **7,173 input records**.
- A separate local scale check staged **128,000 synthetic records** into 16 windows in **1.78 seconds**, with about **45 MB peak RSS** on macOS. This is one local measurement, not a throughput guarantee.
- The synthetic training example builds 24 train, 6 validation and 6 test examples and trains a reference classifier. These labels carry no sentiment signal; this validates the data path, not predictive accuracy.

Requests are intercepted locally. These checks do not certify live PostHog ingestion or emotion inference. `_batchKey` is an internal SDK option; the two pinned SDKs are a compatibility check, not a guarantee for every version.

## Earlier SDK loading and hot-path investigation

The following measurements predate the v0.2.0 transport-batching change and are retained as investigation history, not current throughput claims.

32 unit tests and the browser suite passed on Node 22.23.3 and Chrome 154.0.8037.98. The CrowdCast trace check again preserved all 7,173 records.

- **Snippet install.** A page using PostHog's official HTML snippet with `recorder.js` loaded while `array.js` was still in flight: v0.1.1 reported 4 records submitted and delivered 0 requests (a control `posthog.capture()` on the same page was delivered). The fixed build delivered the batch. This now runs in `npm run test:browser`.
- **Client rate limit.** 200 recorder batches in a burst no longer exhaust the SDK's shared budget: none were refused and a following host event was still accepted.
- **Plain HTTP.** v0.1.1 threw `crypto.randomUUID is not a function` on a non-secure origin; the fixed build starts.
- **Hot-path cost.** 3,000 dispatched pointer events × 8 coalesced samples on an input 15 elements deep, with SDK default persistence, median of 5 runs:

| Measurement | v0.1.1 | Fixed |
| --- | ---: | ---: |
| Recorder only (real SDK consent/identity reads, no-op capture), µs per sample | 5.91 | 2.85 |
| End to end with posthog-js 1.438.7, µs per sample | 11.56 | 8.17 |

With the pinned posthog-js 1.280.0, end to end is slower (12.4 vs 8.4 µs per sample in this compressed burst) because that SDK version spends roughly 0.9 ms more per `capture()` call that has an options object, and the fixed build always passes `skip_client_rate_limiting`. At about one batch per second of continuous movement that is under 0.1% of a CPU core. Earlier comparisons that let 1.280.0's rate limiter refuse batches made v0.1.1 look cheaper; the limiter was lifted on the SDK object for these runs, because 1.280.0 ignores the `rate_limiting` init option.

## Automated unit tests (v0.1.1)

26 tests passed on Node 22.23.3 (v0.1.1):

- Key down/repeat/up and fractional timing; physical code only, no `event.key`.
- A constructed 120 Hz coalesced stream retained exactly, without the parent duplicate.
- Raw-update selection excludes duplicate pointermove capture.
- 1,000 samples through small byte/count caps with no dropped or duplicated samples.
- Password/private ancestors including composed shadow paths.
- Opt-out at flush and permission callback failures.
- Identity transitions discard old-identity pending records.
- Timing-only and keyboard-off modes.
- Burst splitting, lifecycle boundaries, beacon option selection, stop cleanup.
- Synthetic-event filtering, SDK exceptions, and idle timer flushing.
- SDK identity getter failures, cleanup, and recovery; absent session IDs are omitted.
- Exclusions and observed consent interruptions break key pairing.
- Analyzer precision, even-sized medians, retry deduplication, out-of-order delivery, sequence gaps, and recorder isolation.

## Browser integration

Passed in Chrome 154.0.8037.98 using Playwright 1.56.1:

- Browser-trusted keyboard down/up, pointer down/up, click and movement.
- Password input exclusion, including keyboard and outside pointer movement while an open shadow-root password field is focused.
- Single-script auto-start against the real PostHog JS SDK 1.280.0.
- Parsed SDK payloads retained the exact down/up key sequence, schema metadata, and matching SDK session/person identity.
- The demo server refused access to private fixtures, `.env`, and arbitrary repository files.

## Synthetic dispatch stress

Ten seconds of synthetic event timestamps at each rate went through actual Chrome DOM listeners. No dropped samples, timestamp changes, coordinate changes, or ordering differences were observed:

| Timestamp rate | Samples | Recorder properties JSON bytes | Dispatch + capture wall time |
| --- | ---: | ---: | ---: |
| 120 Hz | 1,200 | 308,358 | 13.1 ms |
| 1,000 Hz | 10,000 | 2,493,051 | 80.4 ms |
| 8,000 Hz | 80,000 | 20,340,415 | 600.9 ms |

These are one local run with a synchronous in-memory sink, including event construction and JSON serialization. They do not measure SDK/network throughput, production frame latency, hardware sampling, or sustained real-time performance. The test verifies retention at high sample volumes; rates describe synthetic timestamps, not observed device input.

## SDK integration scope

SDK network requests were intercepted on localhost. This confirms client-side SDK integration, not ingestion by a live PostHog project. Only the headless SDK test disables PostHog's bot/user-agent filter; production configuration is not changed.

Both SDK test dependencies are pinned. Set `POSTHOG_SDK=posthog-js-current` to exercise 1.438.7; the default is 1.280.0. Compatibility with other SDK releases has not been exhaustively tested.

## CrowdCast trace validation

One existing local CrowdCast msgpack segment was read without modifying the recorder. The private fixture contained:

| Type | Count |
| --- | ---: |
| Mouse moves | 6,890 |
| Mouse presses | 35 |
| Mouse releases | 35 |
| Key presses | 106 |
| Key releases | 107 |
| Redaction boundaries | 13 |

All **7,173 input records** retained their event kinds, order, and timestamps through synthetic DOM dispatch and the recorder's normal batching. Thirteen redactions were handled as boundaries. Six source records lacked a raw timestamp and were skipped; scroll records were outside this fixture's supported subset.

This is a trace-driven data-path test, not live desktop capture or proof of a hardware/browser 120 Hz rate. It deliberately uses CrowdCast's `raw_timestamp_us`; the video-aligned clock would quantize timing. Original key codes, coordinates, filenames, and raw packets are not published.

## Remaining limits

- No guarantee of 120 Hz on any particular device/browser; no interpolation is used.
- No Safari, Firefox, mobile, cross-origin iframe, or large-production-site performance certification.
- No real-project PostHog ingestion test; existing SDK delivery rules and limits apply.
- Browser termination can lose pending data. Beacon delivery is best effort.
- The script does not modify native PostHog replay data or native flags.
