import { createRecorder } from './index.js';
export { createRecorder } from './index.js';

// A single script tag after PostHog init. The HTML snippet first installs a placeholder whose methods only
// queue calls, and the SDK replaces window.posthog when it loads, so wait for the loaded instance itself.
if (typeof window !== 'undefined' && !window.__posthogBehavioralLoading) {
  window.__posthogBehavioralLoading = true;
  const started = Date.now();
  let warned = false;
  const start = () => {
    const posthog = window.posthog;
    if (posthog?.__loaded === true && !Array.isArray(posthog._i)) {
      try { window.posthogBehavioral = createRecorder(posthog, window.posthogBehavioralOptions ?? {}); }
      catch (error) { console.error('[posthog-behavioral-recorder]', error); }
      return;
    }
    const waited = Date.now() - started;
    if (waited >= 30000 && !warned) { warned = true; console.warn('[posthog-behavioral-recorder] Still waiting for the PostHog SDK to load'); }
    // Consent-gated init can be late: keep waiting, but back off after the first 30 seconds.
    window.setTimeout(start, waited < 30000 ? 100 : 1000);
  };
  start();
}
