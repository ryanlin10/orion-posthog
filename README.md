# Orion

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/ryanlin10/orion-posthog/actions/workflows/ci.yml/badge.svg)](https://github.com/ryanlin10/orion-posthog/actions/workflows/ci.yml)

Fine-grained input telemetry on top of PostHog, sentiment classifiers trained from it, and live labels for your agents.

- **Connect.** One script after your PostHog snippet. No SDK fork, same sessions, same consent and opt-out.
- **Capture.** Key press and release timing, every pointer sample the browser delivers (including coalesced and raw
  updates), scrolling and focus changes, as `orion_input` events on the session's replay.
- **Mask.** Keys are masked in the browser. By default Orion keeps when a key went down and came up, never which
  key. Password fields and anything you mark send nothing.
- **Mood.** A built-in mood reading (angry, frustrated, calm, anxious, confused, pleased) from pointer speed, changes
  in direction and clicks: simple hand-written heuristics, the same as the demo on paradigmmachines.ai/orion.
- **Ask.** An optional question in the corner of the page, like a PostHog survey: "How is it going so far?" with two
  positive and two negative answers, picked for the page and the visitor's recent input. Answers become labels.
- **Train.** A YAML recipe declares the target, window, telemetry, labellers, split and model. Orion pulls the
  data from PostHog, labels windows with people's own answers, AI (text people wrote, replay activity) or human
  labellers, splits by person, trains, checks the result against shuffled labels, and versions it.
- **Agents.** The trained classifier runs in each visitor's browser. Every label goes to the page
  (`orion.context()`) and to PostHog (`orion_label` events) for search, webhooks and replays, with plain-words evidence.

Nothing is hosted by Orion except the script itself: training runs on your machine and live labelling runs in your
visitors' browsers.

## Set up with one command

Run this in your web app's folder:

```sh
npx github:ryanlin10/orion-posthog
```

It is a setup wizard with an agent behind it, and nothing changes until you have seen it:

1. It reads the project: framework, package manager, and where PostHog starts. With no PostHog yet, it offers to
   run PostHog's own wizard (`npx @posthog/wizard`) first, then carries on.
2. You choose what the keyboard keeps (timing only, key types, or nothing) and the live readings (mood, the
   self-report question).
3. An agent adds Orion next to `posthog.init`: Orion's own agent with your Anthropic, OpenAI or Gemini key, or Claude
   Code or Codex if you have one signed in (no key needed), or you paste the code yourself. Agents read your source
   files; `.env` files stay closed. Plain HTML sites get Orion's browser files copied in and two script tags.
4. You see every changed file as a diff and keep it or undo all of it. Ctrl-C at any point puts every file back.
5. With a PostHog personal API key (`project:read`, `query:read`) it waits until Orion's first events arrive from
   your browser. It can then add Orion's skills to your coding agents.

Keys you give it are saved in `~/.config/orion/keys.env`, the file `orion init` uses, so the training tools need no
second setup. `--yes` takes every default without questions, `--agent api|claude|codex|manual` picks who edits,
and `--help` lists the rest. The wizard sends nothing to Paradigm Machines.

## Install on your site

```html
<!-- your existing PostHog snippet -->
<script>posthog.init('phc_…', { api_host: 'https://us.i.posthog.com' })</script>
<script src="https://cdn.paradigmmachines.ai/orion.js" defer></script>
```

`orion.js` is also at `https://cdn.paradigmmachines.ai/orion@0.3.0/orion.js` (pin a version in production), or build
it into `dist/` with `npm run build` and serve it yourself. It waits for the PostHog SDK to finish loading (the HTML
snippet first installs a placeholder) and keeps waiting if your `init()` waits for consent. Options go before it:

```html
<script>
  window.orionOptions = {
    mask: 'timing',                                // 'timing' (default), 'types' (letter/digit/space/delete…) or 'off'
    models: ['/orion/checkout-sentiment.json'],    // from `orion deploy`; optional
    mood: true,                                    // the built-in mood reading; optional
    selfReport: { survey: '/orion/checkout-sentiment-survey.json' },   // ask how it is going; optional
    isAllowed: () => window.analyticsConsent === true,
    excludeSelector: '[data-sensitive-workflow]',
  };
</script>
```

With a bundler (`npm i github:ryanlin10/orion-posthog`, which installs as `@paradigmmachines/orion`):

```js
import posthog from 'posthog-js';
import { orion } from '@paradigmmachines/orion';

posthog.init('phc_…', { api_host: 'https://us.i.posthog.com' });
window.orion = orion.connect(posthog, { mask: 'timing', models: ['/orion/checkout-sentiment.json'] });
```

The returned object has `context()`, `contextText()`, `labels()`, `onLabel(fn)`, `mood()`, `ask()`, `pause()`,
`resume()`, `flush()`, `stop()`, `getStats()` and `ready` (a promise of the model names that loaded).

### The question (self-report)

`selfReport: true` asks once per page load, after 45 seconds, while the visitor is using the page (never mid-typing
or mid-click), at most once every 14 days per browser. It offers two positive and two negative answers, in random
order, chosen in this order of preference:

1. a survey file from `orion survey`, where Jev (TypeSafe's decision model) ranked the answers ahead of time for each
   page and each mood reading, so no key or server is needed when the question is shown;
2. your own `rank` function, for example a call to your backend;
3. the built-in mood reading.

Settings: `question`, `positive`, `negative`, `trigger` (`'time'`, `'mood'` to ask once the mood reading has been
negative for 3 seconds, or `'manual'`), `afterMs`, `sample`, `cooldownDays`, `pages`, `position`, `labels` (display
text) and `theme`. `orion.ask()` asks right away. Orion's own capture ignores the question, so answering it never
becomes telemetry, and the answer labels the input before the question appeared.

### What is sent

| Event | When | Contents |
| --- | --- | --- |
| `orion_session_start` | once per page load | Orion version, mask, pointer source, model names, whether mood and the question are on |
| `orion_input` | every few seconds of activity | Input records ([format](docs/data-format.md)), batched to keep the event count low |
| `orion_label` | when a live label changes, and every 60 s while it holds | model, version, label, confidence, plain-words evidence, agents |
| `orion_self_report` | when someone answers or closes the question | the answer, the four answers shown and their order, the recorder and the moment the question appeared |

The mood reading sends `orion_label` events as model `orion-mood` once a new mood has held for 2 seconds; with
`selfReport` alone it runs in the page only and sends nothing.

Never sent: key identities (in the default mask), typed text, field values, clipboard, DOM text or selectors.
Password fields, `.ph-no-capture`, `.ph-sensitive`, `[data-private]`, `[data-ph-behavior-ignore]` and your
`excludeSelector` are silent, and all input pauses while one has focus. Input timing can still identify a person,
so treat it as personal data. `has_opted_out_capturing()` and your `isAllowed` are checked before capture, before
any label is sent and before the question is shown. Call `pause()` when consent is withdrawn.

PostHog bills per event, so Orion sends larger batches every few seconds instead of one per burst. Live labels
are computed in the page, so they do not wait for uploads.

## Train a classifier

```sh
pip install "paradigm-orion[sequence] @ git+https://github.com/ryanlin10/orion-posthog"   # Python 3.11+
orion init          # asks for the PostHog key and project, the AI labeller (Anthropic, OpenAI or Gemini) and its key,
                    # and a TypeSafe key for Jev; checks each one
orion pull --since 30d --recipe recipes/checkout-sentiment.yaml
orion label human recipes/checkout-sentiment.yaml     # optional: a local labelling page with replay links
orion train recipes/checkout-sentiment.yaml           # labels, builds a dataset version, trains, reports
orion survey recipes/checkout-sentiment.yaml          # optional: rank the question's answers with Jev
orion deploy checkout-sentiment --context support-agent       # into public/orion, with the snippet to paste
```

`orion init` saves settings to `.orion/config.json` and keys to `~/.config/orion/keys.env`, readable only by you;
environment variables (`POSTHOG_PERSONAL_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`TYPESAFE_API_KEY`) win over the file. The PostHog key needs `query:read`, `project:read`, `batch_export:read` and
`batch_export:write`. The AI labeller defaults to `claude-sonnet-5-5`, `gpt-5.6-sol` or `gemini-3.8-flash`; a
recipe's `labels.model.llm` overrides it.

The recipe format is in the [orion-datasets skill](skills/orion-datasets/SKILL.md) and an example in
[recipes/checkout-sentiment.yaml](recipes/checkout-sentiment.yaml).

- **Pull** uses PostHog's file download exports (one week per export), as PostHog asks of tools that read events in
  bulk. The query API is used only for one session's labels and for label search.
- **Labellers.** *Self-report* reads people's own answers to the question. *Text* reads what people wrote (by
  default PostHog LLM analytics `$ai_generation` events, or a file) and labels the input while they wrote it, judging
  each message against the person's own baseline. *Replay* reads the session's page views, clicks with element text,
  rage and dead clicks, errors and survey answers, and labels fixed windows. *Human* serves a page at 127.0.0.1 with
  each window's input strip and a link to the replay at that moment. AI answers are cached; where labels overlap,
  people's own answers beat human labels, which beat text labels, which beat replay labels.
- **Training** splits by person by default, so validation is people the model never saw. Every report compares the
  model with always guessing the most common label and with the same model trained on shuffled labels, in a
  plain summary line, and includes the training and validation loss curve (per epoch, or per round of trees).
- **Models.** Boosted trees, random forests and logistic regression on window features run in the browser as
  plain JavaScript (orion.js is 40 KB, 16 KB compressed, including capture, mood and the question). GRU,
  temporal CNN and transformer models read 100 ms time steps with the channels of the recipe's telemetry
  (pointer: moves, path, peak speed, turning, presses, releases, hold; keyboard: downs, ups, hold; scroll: wheel;
  focus: changes; plus capture gaps and activity), are exported to ONNX and run with onnxruntime-web.

In Python:

```python
from paradigm_orion import Orion

orion = Orion(posthog_project="acme-web")
clf = orion.train("recipes/checkout-sentiment.yaml")
print(clf.summary)
clf.deploy(context=["support-agent"])
```

## Labels for agents

```sh
orion context <session_id>                 # YAML block for a prompt; --json for structured
orion search --label frustrated --page /checkout --last 30d
orion search --label frustrated --cohort "Paying customers"    # a PostHog cohort, by name or id
```

```js
// In the page, instantly:
fetch('/api/assistant', { method: 'POST', body: JSON.stringify({ message, orion: window.orion.contextText() }) });
```

Skill files in [skills/](skills/) teach coding and research agents the CLI: `orion-context`, `orion-research`,
`orion-datasets`, `orion-train` and `orion-deploy` (`npx skills add ryanlin10/orion-posthog`).

## Try it without a PostHog project

```python
from paradigm_orion.synthetic import generate, write_export
rows, truth = generate(people=30)
write_export(rows, "synthetic.jsonl.gz")
```

```sh
orion pull --file synthetic.jsonl.gz --text-event '$ai_generation'
orion train recipes/checkout-sentiment.yaml
```

The generated people have a planted calm/frustrated signal; it tests the pipeline, not people.

## Develop and test

```sh
npm ci && npm run build
npm test                      # JS unit tests, including feature parity with the Python builder
npm run test:browser          # Chrome + the real PostHog SDK (POSTHOG_SDK=posthog-js-current for the latest)
python3.13 -m venv .venv && .venv/bin/pip install -e '.[sequence]' onnxruntime
npm run test:python           # builder, labellers, providers, export parity, end-to-end on generated data
```

The browser computes the same window features and time steps as the Python builder, and runs exported models
with the same outputs as scikit-learn and PyTorch; tests check both on generated data.

Independent project; not affiliated with PostHog.

## License

[MIT](LICENSE) © 2026 Ryan Lin
