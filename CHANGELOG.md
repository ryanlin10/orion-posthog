# Changelog

## Orion 0.4.0 (unreleased)

Input schema 1.4. The new records are on by default; the features that use them are opt-in recipe families, so
existing recipes and deployed models are unchanged.

- Field fills: an `input` record when a field changes without typing, with how (`paste`, `drop` or `fill` for
  autofill) and the coarse field kind, never the value or its length. Typing, deleting and undo make no record, so
  nothing leaks that the timing mask hides. `fills: false` turns it off. Family `fills`: `paste_count`, `autofill_count`.
- Time away: a `visible` record when the page comes back, with `hidden_ms`; it travels with the next input like a
  waiting boundary, so a return alone sends nothing. Family `visibility`: `hidden_count`, `hidden_ms`; recipes with it
  keep windows in which the person left the tab (blur, hidden and pagehide no longer count as capture gaps there).
- Scroll depth: page scrolls (and scrolling areas at least half the window tall) carry `content_height` and
  `view_height`; batches carry `viewport.doc_height`. Family `depth`: `scroll_depth_max`, a share of the page.
- Overshoot: a press on a link, button, field or button-like element carries that control's box (`box_x`, `box_y`,
  `box_w`, `box_h`). Family `overshoot`: `overshoot_fraction` (presses where the pointer reached the control, left it
  by 2 px or more and came back) and `overshoot_px_median`.
- Correction rate: family `corrections`, `correction_rate` (deletes per key press). It needs `mask: 'types'`; the
  builder also derives key classes from `code`. `orion deploy` prints `mask: 'types'` for such models and
  `connect()` warns when the mask hides them.
- Per-person baselines: `features: {baseline: true}` adds `<feature>_rel`, each window against the same browser's
  earlier page loads (PostHog `$device_id`), for tree and logistic models. orion.js keeps the matching running means
  in `localStorage` (`orion-baseline:<model>`, averages of window features only, written while capture is allowed), and
  the builder computes the same means from pulled history; a parity test checks they agree.
- Outcome labels from goal events the site already tracks: `labels.by: [outcome]` with
  `labels.outcome: {events: {purchased: [order completed, /checkout/done]}, otherwise: left, within, skip, windows}`.
  The first goal in a session labels the windows before it; `orion pull --recipe` pulls the goal events;
  `orion label outcome`. Priority: self-report, human, outcome, text, replay.
- The setup skill's first recipe uses the new families, lists the app's goal events for later, and leaves the mask
  at timing.
- Base model: `connect(posthog, { base: true })` runs a model that ships inside orion.js, so a site has a reading before
  it trains its own: model `orion-base`, `neutral` or `negative`, from typing rhythm and pointer movement shape over the
  last 10 seconds. Logistic regression trained on the public DUX dataset (46 people, a web form with frustration
  triggers, observer-annotated emotion; CC BY 4.0, see NOTICE). On held-out people: AUC 0.69 (0.58 within the same form
  step); it labels 10% of windows `negative` and is right 25% of the time against 13% by chance. It did not predict
  self-reported mood in everyday computer use (Freihaut and Göritz field study, 174 people: AUC 0.50), and no model
  trained on that study beat chance. docs/base-model.md has the comparison of architectures and data, and
  `scripts/base_model.py` rebuilds it. `context()` prefers a site's own model, then the base model, then the mood
  reading. The setup wizard and skill turn it on.
- Research features: families `typing` (press-to-press, release-to-press and hold quartiles; pauses), `motion` (per
  movement: directness, deviation, crossings, direction consistency, turning, speed profile, where the peak falls, late
  against early speed, 100 ms jitter; read at most every 40 ms so sampling rates compare) and `touch` (contact area,
  real pressure). `overshoot_px_sd`. Touch and pen samples record their contact size.
- Models can carry a decision rule (`decision: {class, min_probability}`): a rare class wins from its own bar.
- `orion train` is the whole loop in one command: it pulls what is new from PostHog, labels, builds, trains and checks.
  The recipe is optional when `recipes/` holds one. `--deploy` also deploys, only when the model has validation
  examples and beat shuffled labels (p at most 0.05), and exits 1 otherwise; `--json` prints one result object for
  agents with progress on stderr; `--no-pull` trains on data already here. `Orion.train()` pulls too
  (`pull=False` to skip), and `Classifier.deploy_check()` says why a model is not ready.
- One shipped recipe, `recipes/site-feelings.yaml`, identical to the one the setup skill writes (a test keeps them
  so): self-report, human and AI replay labels with a 0.6 confidence floor, logistic regression (it beats trees until
  a few hundred labels), and windows that need 20 input records. Removed: `recipes/checkout-sentiment.yaml` (AI proxy
  labels only, a pinned Anthropic model, a two-class target that wasted most answers to the question), the builder
  examples `sentiment.json`, `multitask.json` (targets `orion train` cannot train) and `implicit.json` (one
  `features.exclude` line), the reference trainer `examples/training/train_classifier.py` with `logistic.json` (a weaker
  copy of `orion train`), and the synthetic `make_demo.py`, whose labels were unrelated to its input. Recipes are no
  longer in the npm package.

## Orion 0.3.0

Numbered after the recorder's 0.1.0 and 0.2.0 releases in this repository, which is now `ryanlin10/orion-posthog` (the old `posthog-behavioral-recorder` address redirects).

- Built-in mood reading (`mood: true`): angry, frustrated, calm, anxious, confused or pleased from pointer speed, changes in direction and clicks, with the same heuristics as the demo on paradigmmachines.ai/orion. Labels go to `context()`, `onLabel()` and PostHog as model `orion-mood`; `orion.mood()` reads it.
- Self-report (`selfReport`): a question in the corner of the page asks how it is going, with two positive and two negative answers picked for the page and the visitor's recent input, and sends `orion_self_report`. Answers become `self_report` labels for the input before the question; they win over every other labeller. `orion.ask()` asks now.
- `orion survey <recipe>`: Jev (TypeSafe) ranks the answers for each page and mood ahead of time into a survey file that `orion deploy` ships; without a TypeSafe key the AI labeller's model ranks them.
- `orion init` signs in to PostHog in the browser (OAuth with PKCE, like PostHog's own CLI): PostHog's page asks which project, and the sign-in carries exactly the scopes `orion pull` needs, so there is no key to paste and no scope to forget. Tokens refresh by themselves and live in `~/.config/orion/posthog-auth.json` (mode 600). A personal API key still works; its scopes are checked when it is entered or found, and after fixing them in PostHog it is checked again without pasting it twice.
- `orion init` from an agent's shell opens a setup page on 127.0.0.1 in the person's browser to sign in and add keys, so keys never pass through the agent or a chat. In a terminal it asks there (`--browser` or `--terminal` to choose).
- AI keys are checked with one tiny request to the model they will label with, so a wrong key, an account without credit or a model the key cannot use shows up during setup, with the page that fixes it. Keys live in `~/.config/orion/keys.env` (mode 600). The AI labellers run on Anthropic, OpenAI or Gemini.
- `orion check` (and `--json`): the folder, PostHog access, the AI key and the TypeSafe key, live, each with its fix.
- `orion pull` checks PostHog access before it creates any export, and in a terminal offers to sign in and carry on.
- Orion finds `.orion/` in a parent folder (like git) and refuses to set up in the home folder or at the top of the disk.
- The setup wizard's last step signs in to PostHog the same way and saves the sign-in for the training tools; it warns when the project chosen is not the one the app sends to.
- `orion search --cohort <name or id>`.
- Sequence models read only the channels of the recipe's telemetry families (they used every channel before). Reports include the training and validation loss curve.
- `orion deploy` writes to `public/orion` unless told otherwise and prints a snippet that loads `https://cdn.paradigmmachines.ai/orion@0.3.0/orion.js`.
- Recipes accept `temporal_cnn` for `tcn`, and session alongside user or day in `split.by` (it adds nothing there). An unset `labels.model.llm` uses the model chosen at `orion init`.
- Fixes: `Orion(posthog_project="<name>")` looks the project up without a project id set; a 400 from an AI provider fails that request instead of the whole labelling run.
- Setup wizard, `npx github:ryanlin10/orion-posthog`: reads the project, hands over to PostHog's wizard when PostHog is missing, asks what to record, installs the package (or copies the browser files into a plain HTML site), and has an agent add `orion.connect` next to `posthog.init`. The agent is Orion's own (Anthropic, OpenAI or Gemini, on your key; saved keys are checked first) or Claude Code or Codex if one is signed in. Every change is shown as a diff to keep or undo; Ctrl-C puts every file back. It then waits for the first Orion events in PostHog and offers the skills.
- `connect()` called again for the same PostHog client returns the running connection, so React's double effects and hot reloads no longer record everything twice. `stop()` it to reconnect with other options.

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
