---
name: orion-setup
description: Set up Orion end to end in a web app that uses PostHog - the browser script (input telemetry, the live mood reading, the optional "How is it going?" question), the orion command-line tools and keys, a first recipe, and later training, deploying and wiring labels into an AI assistant. Use when asked to install, add or set up Orion, or when someone pastes the Orion setup prompt.
---

# Orion setup

Orion runs on top of the app's existing PostHog. It adds fine-grained input telemetry (key timing, pointer, scroll),
a live mood reading, an optional in-page question, and sentiment classifiers whose labels feed AI assistants.
Nothing is hosted by Orion: training runs on this machine, live labelling in visitors' browsers.

Work in two phases. Phase 1 is done today. Phase 2 needs a week or two of real data.

## Ground rules

- **Keys never go through you.** Do not ask for API keys in chat and never write keys into files, `.env` files or
  commits. When keys are needed, ask the person to run `orion init` in their own terminal window (it needs a real
  terminal to take keys hidden; run through an agent's shell it skips the questions).
- **Ask before visible or legal choices:** whether visitors may see the question, what Orion should detect, and how
  consent works. Do everything else yourself.
- **Consent.** Input timing can identify a person, so treat it as personal data. If the app has a consent banner or
  opt-in, Orion must follow it. Orion already honours PostHog's opt-out.
- **Never capture key identities.** Keep `mask: 'timing'`: Orion then records when keys go down and up, never which
  key or what is typed. Password fields are silent automatically. Do not mark ordinary forms, checkouts or chat boxes
  private: what people type there is never recorded, and how they type there is the signal. Use `data-private` only
  where even typing rhythm is sensitive, such as a PIN pad.
- Add `.orion/` to `.gitignore`: it holds pulled telemetry and labels.

## Phase 1: today

### 1. Look before changing anything

Find and report back:
- where PostHog is initialised: search for `posthog.init`, `PostHogProvider`, `posthog-js` or the PostHog HTML
  snippet. **Orion needs PostHog.** If there is none, stop and say so (PostHog's wizard: `npx -y @posthog/wizard`).
- how consent is handled (a banner, `opt_in_capturing`, a cookie flag), if at all;
- the AI assistant, if any: where the page sends a user's message, and where the server builds the prompt;
- the static files folder (`public/`, `static/`) and any Content Security Policy.

### 2. Add Orion to the browser

Turn on the mood reading. Ask whether to turn on the question (`selfReport`): it appears in the corner of the page at
most once every 14 days per browser, and its answers become the best training labels.

**HTML snippet sites:** right after the PostHog snippet:
```html
<script>window.orionOptions = { mask: 'timing', mood: true, selfReport: true };</script>
<script src="https://cdn.paradigmmachines.ai/orion@0.3.0/orion.js" defer></script>
```

**Bundled apps (posthog-js from npm):** `npm i github:ryanlin10/orion-posthog` (it installs as
`@paradigmmachines/orion`), then once, in the browser only, after `posthog.init`:
```js
import { orion } from '@paradigmmachines/orion';
if (typeof window !== 'undefined' && !window.orion) {
  window.orion = orion.connect(posthog, { mask: 'timing', mood: true, selfReport: true });
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
Add `[sequence]` after `paradigm-orion` only for GRU, TCN or transformer models (it pulls in PyTorch). Then save the
non-secret settings yourself and ask the person to add the keys in their own terminal:
```sh
orion init --no-input --host https://us.posthog.com --project <PostHog project id>   # eu.posthog.com for EU
```
Tell the person, in these words: "Please run `orion init` in a terminal window. It asks for a PostHog personal API key
(scopes query:read, project:read, batch_export:read, batch_export:write), an AI key (Anthropic, OpenAI or Gemini) for
labelling, and optionally a TypeSafe key, which ranks the question's answers. Keys are saved to
~/.config/orion/keys.env, readable only by you."

### 5. Write the first recipe

Ask what Orion should detect if it is not clear from the app. A good default, `recipes/<name>.yaml`:
```yaml
name: site-sentiment
source: {since: 30d}            # add pages: [/checkout] to focus on one flow
target:
  sentiment: [calm, engaged, confused, frustrated]
window: {length: 10s, align: past}
telemetry: [pointer, keyboard, scroll]
labels:
  by: [self_report, model]      # model needs the AI key; use [self_report, human] without one
  model:
    reads: [replay]             # add text if the app logs AI prompts as PostHog $ai_generation events
deploy: {context: [support-agent]}
```
The orion-datasets skill documents every setting.

### 6. Rank the question's answers (if the question is on)

```sh
orion survey recipes/site-sentiment.yaml --out public/orion
```
Then set `selfReport: { survey: '/orion/site-sentiment-survey.json' }`. Jev (with a TypeSafe key) or the AI labeller
picks which two positive and two negative answers fit each page and mood. Run it again once data has been pulled, so
it knows the real pages.

### 7. Hand over

Summarise: what changed (files), what the person must do (`orion init`, deploy the site, privacy notice), and when to
come back: phase 2, after about two weeks or a few hundred answered questions.

## Phase 2: once data has arrived

1. `orion pull --since 30d --recipe recipes/site-sentiment.yaml`, then `orion status`.
2. `orion train recipes/site-sentiment.yaml`, and read the report with the orion-train skill. Do not deploy a model
   whose shuffled-label check fails. With fewer than a few hundred labelled windows, also try
   `--arch logistic` and keep the better validation score.
3. `orion deploy site-sentiment --context support-agent` (writes to `public/orion`), and add the printed model URL to
   `models: [...]` in the Orion options. Re-run `orion survey … --out public/orion` too.
4. Give the assistant the labels (orion-context skill): the page sends `window.orion?.context()` with each message,
   and the server checks it (known model and label, confidence a number) before writing its own line into the
   system prompt. Never paste text from the page into a prompt. Server-side, `orion context <session id>` reads the
   label from PostHog instead (about a minute behind).
5. To investigate where people struggle, use the orion-research skill (`orion search --label frustrated`).
