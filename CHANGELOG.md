# Changelog

## Orion 0.3.0 (unreleased)

Numbered after the recorder's 0.1.0 and 0.2.0 releases in this repository, which is now `ryanlin10/orion-posthog` (the old `posthog-behavioral-recorder` address redirects).

- Built-in mood reading (`mood: true`): angry, frustrated, calm, anxious, confused or pleased from pointer speed, changes in direction and clicks, with the same heuristics as the demo on paradigmmachines.ai/orion. Labels go to `context()`, `onLabel()` and PostHog as model `orion-mood`; `orion.mood()` reads it.
- Self-report (`selfReport`): a question in the corner of the page asks how it is going, with two positive and two negative answers picked for the page and the visitor's recent input, and sends `orion_self_report`. Answers become `self_report` labels for the input before the question; they win over every other labeller. `orion.ask()` asks now.
- `orion survey <recipe>`: Jev (TypeSafe) ranks the answers for each page and mood ahead of time into a survey file that `orion deploy` ships; without a TypeSafe key the AI labeller's model ranks them.
- `orion init` asks for and checks the PostHog key and project, the AI labeller (Anthropic, OpenAI or Gemini) and its key, and a TypeSafe key; keys live in `~/.config/orion/keys.env` (mode 600). The AI labellers run on any of the three providers.
- `orion search --cohort <name or id>`.
- Sequence models read only the channels of the recipe's telemetry families (they used every channel before). Reports include the training and validation loss curve.
- `orion deploy` writes to `public/orion` unless told otherwise and prints a snippet that loads `https://cdn.paradigmmachines.ai/orion@0.3.0/orion.js`.
- Recipes accept `temporal_cnn` for `tcn`, and session alongside user or day in `split.by` (it adds nothing there). An unset `labels.model.llm` uses the model chosen at `orion init`.
- Fixes: `Orion(posthog_project="<name>")` looks the project up without a project id set; a 400 from an AI provider fails that request instead of the whole labelling run.

## Orion 0.1.0

Orion grows out of posthog-behavioral-recorder 0.2.0 (history kept). Package names: `@paradigmmachines/orion` (npm) and `paradigm-orion` (PyPI, `orion` command).

- Browser: `orion.connect(posthog, options)` and `orion.js` (script tag). The default mask is now timing only; `types` adds a key class; `code` stays opt-in. Focus changes are recorded with a coarse field kind. Events are `orion_session_start`, `orion_input` (was `behavioral_burst`, schema 1.3) and `orion_label`. Batches default to columns, every 5 s or 2 s of idle, to cut PostHog event counts.
- Live labels in the browser: models from `orion deploy` score the latest window every 2 s, with the training builder's window rules, a two-score confirmation before switching, and plain-words evidence. `context()`, `contextText()`, `labels()`, `onLabel()`. Labels clear on consent loss, pause and session changes.
- Browser features and time steps match the Python builder exactly; exported trees, forests, logistic models and ONNX sequence models give the same outputs in the browser as in scikit-learn and PyTorch.
- `orion` command: `init`, `pull` (PostHog file download exports, a week at a time), `label text|replay|human`, `build`, `train`, `deploy`, `context`, `search`, `status`. YAML recipes.
- Labellers: AI on text people wrote (default PostHog `$ai_generation`), AI on replay activity, and a local human labelling page with replay links. Cached AI answers; overlaps resolved human > text > replay.
- Training: logistic, random forest and boosted trees; GRU, temporal CNN and transformer (PyTorch → ONNX → onnxruntime-web). Validation on held-out people by default, a most-common-label baseline and a shuffled-label check in every report.
- Five skill files for agents: orion-context, orion-research, orion-datasets, orion-train, orion-deploy.
- Generated data with a planted signal (`paradigm_orion.synthetic`) and an end-to-end test against a stand-in PostHog server.

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
