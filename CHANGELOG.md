# Changelog

## 0.2.0

- Fix: with PostHog's HTML snippet, the auto-start script attached to the snippet's placeholder, which the SDK replaces on load, so no batch ever reached PostHog while stats reported them as submitted. It now waits for the loaded SDK, and `createRecorder` rejects the placeholder.
- Fix: recorder batches skip the SDK's shared client rate limit, which could refuse them and starve the host application's own events.
- Fix: batches the SDK refuses are counted as dropped and leave a `capture-gap` boundary (posthog-js only; capture-only wrappers are unchanged).
- Fix: recorder IDs fall back to `crypto.getRandomValues()`, so plain-HTTP pages no longer fail to start.
- Auto-start keeps waiting after 30 seconds (every second) so consent-gated `init()` works.
- Lifecycle boundaries with nothing buffered ride on the next input instead of producing input-free events on every blur or tab switch.
- Schema 1.2 (including the 1.1 clock addition): each chunk carries a `clock` anchor (`Date.now()` and `performance.now()`) for aligning samples with replay timestamps.
- Faster hot path: consent and identity are read once per dispatched event instead of per coalesced sample; exclusion checks are linear in DOM depth; one idle timer per burst; allocation-free byte counting.

- Preserve SDK transport batching while bypassing the shared client rate limit; test against posthog-js 1.280.0 and 1.438.7.
- Stop processing coalesced packets after synchronous stop/pause hooks; schedule idle timers using a monotonic clock.
- Add opaque per-press IDs in timing mode, recorder byte budgets, per-batch quality counters, and optional lossless columnar JSON with a public decoder.
- Add a standard-library Python dataset builder: custom target schemas, interval/sliding windows, feature hooks and ablations, grouped splits, strict label provenance, disk-backed staging, checksums and rejection reports.
- Include editable recipes, a synthetic end-to-end demo and an optional reference classifier with train-only preprocessing and a class-prior baseline.

## 0.1.1

- Preserve relative timestamp precision in analysis and correct even-sized medians.
- Deduplicate and order batches by recorder/sequence; break pairing across missing batches.
- Mark excluded input and observed consent/SDK interruptions as continuity gaps.
- Respect focused password/private elements inside nested open shadow roots.
- Handle SDK identity lookup failures without crashing timers or preventing cleanup.
- Omit unavailable session IDs instead of overriding SDK enrichment with null.
- Restrict the local demo server to its public assets.
- Add regression coverage, parsed real-SDK payload assertions, high-volume DOM dispatch tests, and repeat the 7,173-event CrowdCast fixture check.

## 0.1.0

- Initial high-resolution browser recorder, PostHog integration, local demo, and CrowdCast trace test harness.
