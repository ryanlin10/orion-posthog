# Contributing

Run `npm ci` and `npm run check`. Install Chromium with `npx playwright install chromium` if Chrome is unavailable.

Keep production runtime dependencies empty. Add regression tests when changing capture, timestamps, privacy exclusions, identity boundaries, or transport. Never commit real telemetry, tokens, browser profiles, or the `artifacts/` folder. Use generated fixtures in tests and aggregate results for real-data validation.

Changes to the wire format should update `schema_version` and `docs/data-format.md`. Preserve raw evidence; do not synthesize releases or interpolate mouse samples and present them as observed events. Keep any later behavioral inference distinct from collection.
