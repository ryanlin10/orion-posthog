---
name: orion-setup
description: Set up Orion end to end in a web app that uses PostHog - the browser script (input telemetry, the live mood reading, the optional "How is it going?" question), the orion command-line tools and keys, a first recipe, and later training, deploying and wiring labels into an AI assistant. Use when asked to install, add or set up Orion, or when someone pastes the Orion setup prompt.
---

# Orion setup

Orion runs on top of the app's existing PostHog. It adds fine-grained input telemetry (key timing, pointer, scroll,
pastes and autofills without their text, time away from the tab, scroll depth, the box of each pressed control), a
live mood reading, an optional in-page question, and classifiers whose labels feed AI assistants.
Nothing is hosted by Orion: training runs on this machine, live labelling in visitors' browsers.

Work in two phases. Phase 1 is done today. Phase 2 needs a week or two of real data.

## Ground rules

- **Keys never go through you.** Do not ask for API keys in chat and never write keys into files, `.env` files or
  commits. Run `orion init` yourself, from the app's folder: in your shell (no terminal) it opens a setup page on
  127.0.0.1 in the person's browser, where they sign in to PostHog with one click and paste the AI key. Each key is
  checked live there and saved to a file only they can read; you only see what worked.
- **Ask before visible or legal choices:** whether visitors may see the question, and how consent works. Do
  everything else yourself, with the defaults below; do not ask what Orion should detect (step 5).
- **Consent.** Input timing can identify a person, so treat it as personal data. If the app has a consent banner or
  opt-in, Orion must follow it. Orion already honours PostHog's opt-out.
- **Never capture key identities.** Keep `mask: 'timing'`: Orion then records when keys go down and up, never which
  key or what is typed. Password fields are silent automatically. Do not mark ordinary forms, checkouts or chat boxes
  private: what people type there is never recorded, and how they type there is the signal. Use `data-private` only
  where even typing rhythm is sensitive, such as a PIN pad. The correction rate (deletes per key press) needs
  `mask: 'types'`, which adds key classes such as letter or delete; switch only if the person asks for it.
- Add `.orion/` to `.gitignore`: it holds pulled telemetry and labels. If the site is deployed by uploading the
  project folder itself (`wrangler pages deploy .`, `netlify deploy --dir .`, an S3 sync of `.`), `.orion/`, recipes
  and skills would be published with it: deploy only the site's own files (move them into `public/` if need be).

## Phase 1: today

### 1. Look before changing anything

Find and report back:
- where PostHog is initialised: search for `posthog.init`, `PostHogProvider`, `posthog-js` or the PostHog HTML
  snippet. **Orion needs PostHog.** If there is none, stop and say so (PostHog's wizard: `npx -y @posthog/wizard`).
- how consent is handled (a banner, `opt_in_capturing`, a cookie flag), if at all;
- the AI assistant, if any: where the page sends a user's message, and where the server builds the prompt;
- goal events the app already sends to PostHog (`posthog.capture('purchase')`, sign-up, cancel) or confirmation
  pages: later, Orion can learn what sessions go on to do from them (phase 2). Just list them; add none;
- the static files folder (`public/`, `static/`) and any Content Security Policy.

### 2. Add Orion to the browser

Turn on the mood reading. Ask whether to turn on the question (`selfReport`): it appears in the corner of the page at
most once every 14 days per browser, and its answers become the best training labels.

**HTML snippet sites:** right after the PostHog snippet:
```html
<script>window.orionOptions = { mask: 'timing', mood: true, base: true, selfReport: true };</script>
<script src="https://cdn.paradigmmachines.ai/orion@0.4.0/orion.js" defer></script>
```

**Bundled apps (posthog-js from npm):** `npm i github:ryanlin10/orion-posthog` (it installs as
`@paradigmmachines/orion`), then once, in the browser only, after `posthog.init`:
```js
import { orion } from '@paradigmmachines/orion';
if (typeof window !== 'undefined' && !window.orion) {
  window.orion = orion.connect(posthog, { mask: 'timing', mood: true, base: true, selfReport: true });
}
```
In React or Next.js put it next to the PostHog init (a client component or `useEffect`), never in server code.
`connect` throws if it runs twice in one window, hence the guard.

**Consent:** pass `isAllowed: () => <the app's consent check>` in the options, and call `window.orion.pause()` when
consent is withdrawn (`resume()` when given). With a strict Content Security Policy, allow
`https://cdn.paradigmmachines.ai` in `script-src` or use the npm install.

Question settings, if wanted: `selfReport: { afterMs: 45000, pages: ['/checkout'], position: 'right' }`;
`trigger: 'mood'` asks only after a few seconds of negative mood. `window.orion.ask()` shows it at once (for testing).

### 3. Check it works

Run the app, use it for a minute, then in the browser console:
- `orion.getStats()`: `recorded` and `submitted` should be climbing; `mask` is `timing`.
- `orion.mood()`: the current mood reading.
- `orion.ask()`, if the question is on: the card appears (answering it sends a real event).
In PostHog's activity view, `orion_session_start` and `orion_input` events arrive within a minute.
Nothing arriving: check consent (`orion.getStats().paused`, `posthog.has_opted_out_capturing()`).

### 4. Install the training tools

Python 3.11 or newer:
```sh
pipx install "paradigm-orion @ git+https://github.com/ryanlin10/orion-posthog"   # or: uv tool install "…same…"
```
Add `[sequence]` after `paradigm-orion` only for GRU, TCN or transformer models (it pulls in PyTorch).

Then, from the app's folder (Orion refuses the home folder), run `orion init` and tell the person, in these words:
"Orion's setup page has opened in your browser. Sign in to PostHog and choose this app's project, add an AI key
(Anthropic, OpenAI or Gemini) for labelling, and optionally a TypeSafe key, then press Finish. The keys stay on your
computer." It waits up to 20 minutes for Finish; if your shell stops commands sooner, run it in the background and
wait for it to exit. Pass `--host https://eu.posthog.com` for PostHog's EU cloud when the app's code does not show it.
If no browser can open (a remote machine), ask the person to run `orion init` in a terminal on their own computer.

Then run `orion check`. Every line should be ✓, or - for an optional part. A ✗ line names its fix; usually that is
running `orion init` again. Do not go on to pulling data until PostHog shows ✓.

### 5. Write the first recipe

Do not ask what Orion should detect. The question already chooses for each visitor: it offers two positive and two
negative answers out of nine feelings, ranked for the page and the visitor's recent input. Train on all nine, so
every answer becomes a label, and say in your summary that the target can be narrowed later. (A target of only some
feelings shrinks the question to those, so every visitor sees the same answers.) The live mood reading's states
(angry, frustrated, calm, anxious, confused, pleased) are a separate built-in reading, not this target.

`recipes/site-feelings.yaml`:
```yaml
name: site-feelings
source: {since: 30d}            # add pages: [/checkout] to focus on one flow
target:
  feeling: [calm, pleased, engaged, confident, frustrated, confused, anxious, angry, bored]
window: {length: 10s, align: past}
telemetry: [pointer, keyboard, scroll, fills, visibility, depth, overshoot]
labels:
  by: [self_report, human, model]   # model needs the AI key; drop it without one
  min_confidence: 0.6           # drops unsure AI labels; answers and human labels always count
  model:
    reads: [replay]             # add text if the app logs AI prompts as PostHog $ai_generation events
model: {architecture: logistic} # beats trees until a few hundred labels; then compare --arch boosted_trees
quality: {min_events: 20}       # a window needs real input, not two stray events
deploy: {context: [support-agent]}
```
The orion-datasets skill documents every setting.

### 6. Rank the question's answers (if the question is on)

```sh
orion survey recipes/site-feelings.yaml --out public/orion
```
Then set `selfReport: { survey: '/orion/site-feelings-survey.json' }`. Jev (with a TypeSafe key) or the AI labeller
picks which two positive and two negative answers fit each page and mood. Run it again once data has been pulled, so
it knows the real pages.

### 7. Hand over

Summarise: what changed (files), what the person must do (deploy the site, a privacy notice; `orion init` only if
`orion check` still shows ✗), and when to
come back: phase 2, after about two weeks or a few hundred answered questions.

## Phase 2: once data has arrived

1. `orion train --deploy --json`: one command pulls the new data, labels it, builds, trains, checks, and writes the
   model to `public/orion` only if it passed (read the result with the orion-train skill). The first time, add the
   printed model URL to `models: [...]` in the Orion options; later runs replace the file in place. Once there are a
   few hundred labelled windows, also try `--arch boosted_trees` and keep the better validation score.
2. Re-run `orion survey recipes/site-feelings.yaml --out public/orion` so the question knows the real pages.
3. Deploy the site as usual so the new files go live.
4. Give the assistant the labels (orion-context skill): the page sends `window.orion?.context()` with each message,
   and the server checks it (known model and label, confidence a number) before writing its own line into the
   system prompt. Never paste text from the page into a prompt. Server-side, `orion context <session id>` reads the
   label from PostHog instead (about a minute behind).
5. To investigate where people struggle, use the orion-research skill (`orion search --label frustrated`).
6. Optional, when the app tracks goals (step 1): an outcome recipe whose labels come from those events, e.g.
   `recipes/checkout-outcome.yaml` with `target: {outcome: [purchased, left]}` and `labels: {by: [outcome], outcome:
   {events: {purchased: [<the app's event>]}, otherwise: left}}` (orion-datasets documents it). Its labels say what a
   session went on to do, not how anyone felt.
7. Optional, when many people come back: `features: {baseline: true}` in the recipe compares each window with the
   same browser's earlier visits; orion.js keeps those averages in the visitor's browser (localStorage).
