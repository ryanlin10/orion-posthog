# PostHog Behavioral Recorder

Capture fine-grained keyboard and pointer telemetry **alongside the existing PostHog browser SDK**. Add one script to your site. No PostHog fork, server plugin, or replacement recorder.

- Key down **and** up, repeat flags, physical key codes, modifiers, and fractional timestamps.
- Pointer down **and** up, clicks, cancellations, button state, pressure, and coordinates.
- Every browser-provided movement sample, including coalesced samples and `pointerrawupdate` when available.
- Small, versioned `behavioral_burst` custom events sent using `posthog.capture()`.
- MIT licensed, no runtime dependencies, ESM and a single-script build.

**120 Hz is a measurement target, not a browser guarantee.** The recorder does not throttle to animation frames or fabricate intermediate points. It keeps all delivered samples, including rates above 120 Hz. Hardware, OS settings, browser support, timer precision, and load can produce lower rates. Use the included analyzer to measure the actual stream.

These are **custom analytics events**. They do not add native rrweb input events, change PostHog's native rage/dead-click flags, or automatically draw movement in its replay player. `$session_id` and per-sample timestamps allow correlation with your existing replay.

## Add one script

Download `recorder.js` from the [v0.1.0 release](https://github.com/ryanlin10/posthog-behavioral-recorder/releases/tag/v0.1.0), serve it from your site, and place this after your normal PostHog initialization:

```html
<script defer src="/vendor/recorder.js"></script>
```

The script waits up to 30 seconds for `window.posthog` to load, then creates `window.posthogBehavioral`. Install separately in each iframe that you own and want to record. Events do not cross iframe boundaries; desktop/global input is outside this script's scope.

Optional configuration must appear before the script:

```html
<script>
  window.posthogBehavioralOptions = {
    keyboard: 'code',
    // Connect this to your own consent state. Pending data is dropped when false.
    isAllowed: () => window.analyticsConsent === true,
    excludeSelector: '[data-sensitive-workflow]'
  };
</script>
<script defer src="/vendor/recorder.js"></script>
```

The existing SDK's `has_opted_out_capturing()` is also checked before collecting and submitting. Call `posthogBehavioral.pause()` immediately when consent is revoked to clear the in-memory buffer. `resume()`, `flush()`, `stop()`, and `getStats()` are available.

## ESM / bundler

The package is available from this repository; it is **not published to npm** by this release.

```sh
npm install github:ryanlin10/posthog-behavioral-recorder#v0.1.0
```

```js
import { createRecorder } from 'posthog-behavioral-recorder';

// Run after your PostHog SDK is initialized (for example, its loaded callback).
const recorder = createRecorder(posthog, { keyboard: 'code' });
// On component/application teardown:
recorder.stop();
```

## What is collected

Physical key codes are recorded by default, **not** `KeyboardEvent.key`, input values, clipboard contents, DOM text, selectors, URLs, or screenshots. **Physical key sequences can still reveal what somebody typed.** Use `keyboard: 'timing'` to omit codes and locations, or `'off'` to disable keyboard capture. Timing mode cannot reliably pair overlapping individual keys.

Password fields, `.ph-no-capture`, `.ph-sensitive`, `[data-private]`, and `[data-ph-behavior-ignore]` are excluded, including matching ancestors in open shadow trees. While an excluded element is focused, all input capture is suppressed. Closed shadow roots cannot expose internal exclusions: mark their host as excluded. The script does not automatically inherit arbitrary rrweb masking settings or custom SDK masking callbacks; mirror those with `excludeSelector` or `isAllowed`.

The PostHog SDK still adds its normal event properties, such as URL and identity; this script's omissions do not disable your SDK's other data collection. Coordinate changes in coalesced packets are attributed to the dispatched event's target; this is not historical hit-testing for every sub-sample.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `keyboard` | `'code'` | `'code'`, `'timing'`, or `'off'` |
| `movementSource` | `'auto'` | Prefer raw updates; fall back to pointermove. Can force either source. |
| `idleMs` | `500` | Idle gap that ends a burst |
| `flushMs` | `1000` | Maximum ordinary buffer residence before submission |
| `maxEvents` | `128` | Maximum records per chunk |
| `maxBytes` | `48000` | Maximum UTF-8 JSON bytes of recorder properties, excluding SDK enrichment |
| `excludeSelector` | — | Additional CSS exclusions |
| `isAllowed` | `() => true` | Your permission/consent gate |
| `allowSynthetic` | `false` | Controlled tests only; never enable just to increase apparent sampling rate |

Pointer events are required. Raw updates/coalescing require supported browsers and generally HTTPS. No `mousemove` compatibility fallback is provided for browsers without Pointer Events. Source selection is capability-based, not a claim that the chosen source actually achieves 120 Hz; force `pointermove` if a browser advertises but does not deliver raw updates.

## Data and analysis

See [the v1 data specification](docs/data-format.md). Burst boundaries represent input inactivity; they do not imply emotion or frustration. Down/up pairs may cross chunks and bursts. Blur, pagehide, and hidden-document boundaries mark uncertainty; no release events are fabricated.

```js
import { analyzeBatches } from 'posthog-behavioral-recorder/analysis';
const result = analyzeBatches(exportedEvents.map(event => event.properties));
console.log(result.keyHoldMs, result.movement);
```

Pass one copy of each batch; the analyzer does not deduplicate ingestion retries. It computes key holds, unmatched releases, and pointer cadence. Movement intervals of 100 ms or longer are excluded from its active-motion cadence statistic; this is a reporting convention, not evidence the user was inactive.

High-resolution telemetry is data-heavy. At 120 samples/second, continuous movement produces 432,000 samples/hour; higher rates produce more. Batching reduces event counts, not the amount of source information. Profile payload volume and PostHog billing on your own traffic before broad rollout. SDK-level filters, consent, network failure, and browser termination can still prevent delivery. `submitted` means handed to the SDK, **not acknowledged by PostHog**. No custom disk queue or background retries are added.

## Develop and test

```sh
npm ci
npm run build
npm test
npx playwright install chromium  # if Chrome is not installed locally
npm run test:browser
npm run demo                    # http://127.0.0.1:4173
```

The demo stores packets in its tab only. Browser tests exercise trusted Chrome input and run the real PostHog SDK with requests intercepted locally. They never submit to a real project. See [validation](docs/validation.md) and [CrowdCast testing](docs/crowdcast.md).

## Sources

- [PostHog custom events](https://posthog.com/docs/libraries/js/usage)
- [Coalesced pointer events](https://developer.mozilla.org/en-US/docs/Web/API/PointerEvent/getCoalescedEvents)
- [Raw pointer updates](https://developer.mozilla.org/en-US/docs/Web/API/Element/pointerrawupdate_event)

Independent community project; not affiliated with PostHog. Contributions should preserve source timestamps, explicit gaps, privacy exclusions, and the distinction between observed input and inferred behavior.
