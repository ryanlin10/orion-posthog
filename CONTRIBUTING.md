# Contributing

Run `npm ci` and `npm run check`, and `npm run test:python` with the `.venv` described in the README. Install Chromium with `npx playwright install chromium` if Chrome is unavailable.

Keep production runtime dependencies empty. Add regression tests when changing capture, timestamps, privacy exclusions, identity boundaries, or transport. Never commit real telemetry, tokens, browser profiles, or the `artifacts/` folder. Use generated fixtures in tests and aggregate results for real-data validation.

Window features (`src/features.js`, `paradigm_orion/dataset/features.py`) and time steps (`src/sequence.js`, `paradigm_orion/sequence.py`) exist twice, for the browser and for training: change both together. The parity tests fail otherwise.

Changes to the wire format should update `schema_version` and `docs/data-format.md`. Preserve raw evidence; do not synthesize releases or interpolate mouse samples and present them as observed events. Keep any later behavioral inference distinct from collection.
