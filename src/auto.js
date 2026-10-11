import { connect } from './index.js';
export * from './index.js';

// A single script tag after PostHog init. The HTML snippet first installs a placeholder whose methods only
// queue calls, and the SDK replaces window.posthog when it loads, so wait for the loaded instance itself.
// Options: window.orionOptions = { mask: 'timing', models: ['/orion/checkout-sentiment.json'] }
if (typeof window !== 'undefined' && !window.__orionLoading) {
  window.__orionLoading = true;
  const started = Date.now();
  let warned = false;
  const start = () => {
    const posthog = window.posthog;
    if (posthog?.__loaded === true && !Array.isArray(posthog._i)) {
      try { window.orion = connect(posthog, window.orionOptions ?? window.posthogBehavioralOptions ?? {}); }
      catch (error) { console.error('[orion]', error); }
      return;
    }
    const waited = Date.now() - started;
    if (waited >= 30000 && !warned) { warned = true; console.warn('[orion] Still waiting for the PostHog SDK to load'); }
    // Consent-gated init can be late: keep waiting, but back off after the first 30 seconds.
    window.setTimeout(start, waited < 30000 ? 100 : 1000);
  };
  start();
}
