# Changelog

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
