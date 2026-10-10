import { createRecorder } from './index.js';
export { createRecorder } from './index.js';

// A single script tag after PostHog init. Wait for the async SDK, not just its stub.
if (typeof window !== 'undefined' && !window.__posthogBehavioralLoading) {
  window.__posthogBehavioralLoading = true;
  const started = Date.now();
  const start = () => {
    if (typeof window.posthog?.get_session_id === 'function' && typeof window.posthog?.get_distinct_id === 'function') {
      try { window.posthogBehavioral = createRecorder(window.posthog, window.posthogBehavioralOptions ?? {}); }
      catch (error) { console.error('[posthog-behavioral-recorder]', error); }
    } else if (Date.now() - started < 30000) window.setTimeout(start, 100);
    else { window.__posthogBehavioralLoading = false; console.warn('[posthog-behavioral-recorder] PostHog SDK did not load'); }
  };
  start();
}
