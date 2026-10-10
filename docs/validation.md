# Validation — 2026-10-10

## Automated unit tests

15 tests passed on Node 22.23.3:

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

## Browser integration

Passed in Chrome 154.0.8037.98 using Playwright 1.56.1:

- Browser-trusted keyboard down/up, pointer down/up, click and movement.
- Password input exclusion.
- Single-script auto-start against the real PostHog JS SDK 1.280.0.
- The SDK serialized a `behavioral_burst` containing keyboard records and a session ID.

SDK network requests were intercepted on localhost. This confirms client-side SDK integration, not ingestion by a live PostHog project. Only the headless SDK test disables PostHog's bot/user-agent filter; production configuration is not changed.

The SDK test version is pinned because the registry's newer resolved package referenced an unavailable dependency at validation time. Compatibility with other SDK releases has not been exhaustively tested.

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
