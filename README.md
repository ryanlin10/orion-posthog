# Orion

Fine-grained input telemetry on top of PostHog, sentiment classifiers trained from it, and live labels for your agents.

- **Connect.** One script after your PostHog snippet. No SDK fork, same sessions, same consent and opt-out.
- **Capture.** Key press and release timing, every pointer sample the browser delivers (including coalesced and raw
  updates), scrolling and focus changes, as `orion_input` events on the session's replay.
- **Mask.** Keys are masked in the browser. By default Orion keeps when a key went down and came up, never which
  key. Password fields and anything you mark send nothing.
- **Train.** A YAML recipe declares the target, window, telemetry, labellers, split and model. Orion pulls the
  data from PostHog, labels windows with AI (text people wrote, replay activity) or people, splits by person,
  trains, checks the result against shuffled labels, and versions it.
- **Agents.** The trained classifier runs in each visitor's browser. Every label goes to the page
  (`orion.context()`) and to PostHog (`orion_label` events) for search, webhooks and replays, with plain-words evidence.

Nothing is hosted by Orion: training runs on your machine and live labelling runs in your visitors' browsers.

## Install on your site

```html
<!-- your existing PostHog snippet -->
<script>posthog.init('phc_…', { api_host: 'https://us.i.posthog.com' })</script>
<script src="/vendor/orion.js" defer></script>
```

`orion.js` is built into `dist/` by `npm run build`. It waits for the PostHog SDK to finish loading (the HTML
snippet first installs a placeholder) and keeps waiting if your `init()` waits for consent. Options go before it:

```html
<script>
  window.orionOptions = {
    mask: 'timing',                                // 'timing' (default), 'types' (letter/digit/space/delete…) or 'off'
    models: ['/orion/checkout-sentiment.json'],    // from `orion deploy`; optional
    isAllowed: () => window.analyticsConsent === true,
    excludeSelector: '[data-sensitive-workflow]',
  };
</script>
```

With a bundler:

```js
import posthog from 'posthog-js';
import { orion } from '@paradigmmachines/orion';

posthog.init('phc_…', { api_host: 'https://us.i.posthog.com', loaded: ph => {
  window.orion = orion.connect(ph, { mask: 'timing', models: ['/orion/checkout-sentiment.json'] });
} });
```

The returned object has `context()`, `contextText()`, `labels()`, `onLabel(fn)`, `pause()`, `resume()`, `flush()`,
`stop()`, `getStats()` and `ready` (a promise of the model names that loaded).

### What is sent

| Event | When | Contents |
| --- | --- | --- |
| `orion_session_start` | once per page load | Orion version, mask, pointer source, model names |
| `orion_input` | every few seconds of activity | Input records ([format](docs/data-format.md)), batched to keep the event count low |
| `orion_label` | when a live label changes, and every 60 s while it holds | model, version, label, confidence, plain-words evidence, agents |

Never sent: key identities (in the default mask), typed text, field values, clipboard, DOM text or selectors.
Password fields, `.ph-no-capture`, `.ph-sensitive`, `[data-private]`, `[data-ph-behavior-ignore]` and your
`excludeSelector` are silent, and all input pauses while one has focus. Input timing can still identify a person,
so treat it as personal data. `has_opted_out_capturing()` and your `isAllowed` are checked before capture and
before any label is sent. Call `pause()` when consent is withdrawn.

PostHog bills per event, so Orion sends larger batches every few seconds instead of one per burst. Live labels
are computed in the page, so they do not wait for uploads.

## Train a classifier

```sh
pip install paradigm-orion                     # Python 3.11+; add [sequence] for GRU/TCN/transformer
orion init --host https://us.posthog.com --project 12345
export POSTHOG_PERSONAL_API_KEY=phx_…          # query:read, batch_export:read, batch_export:write
export ANTHROPIC_API_KEY=sk-ant-…              # only for the AI labellers

orion pull --since 30d --recipe recipes/checkout-sentiment.yaml
orion label human recipes/checkout-sentiment.yaml     # optional: a local labelling page with replay links
orion train recipes/checkout-sentiment.yaml           # labels, builds a dataset version, trains, reports
orion deploy checkout-sentiment --out public/orion --context support-agent
```

The recipe format is in the [orion-datasets skill](skills/orion-datasets/SKILL.md) and an example in
[recipes/checkout-sentiment.yaml](recipes/checkout-sentiment.yaml).

- **Pull** uses PostHog's file download exports (one week per export), as PostHog asks of tools that read events in
  bulk. The query API is used only for one session's labels and for label search.
- **Labellers.** *Text* reads what people wrote (by default PostHog LLM analytics `$ai_generation` events, or a
  file) and labels the input while they wrote it, judging each message against the person's own baseline. *Replay*
  reads the session's page views, clicks with element text, rage and dead clicks, errors and survey answers, and
  labels fixed windows. *Human* serves a page at 127.0.0.1 with each window's input strip and a link to the replay
  at that moment. AI answers are cached; human labels beat text labels beat replay labels where they overlap.
- **Training** splits by person by default, so validation is people the model never saw. Every report compares the
  model with always guessing the most common label and with the same model trained on shuffled labels, in a
  plain summary line.
- **Models.** Boosted trees, random forests and logistic regression on window features run in the browser as
  plain JavaScript (orion.js is 26 KB, 11 KB compressed, including capture). GRU, temporal CNN and transformer models on 100 ms time
  steps are exported to ONNX and run with onnxruntime-web.

In Python:

```python
from paradigm_orion import Orion

orion = Orion(posthog_project="acme-web")
clf = orion.train("recipes/checkout-sentiment.yaml")
print(clf.summary)
clf.deploy(out="public/orion", context=["support-agent"])
```

## Labels for agents

```sh
orion context <session_id>                 # YAML block for a prompt; --json for structured
orion search --label frustrated --page /checkout --last 30d
```

```js
// In the page, instantly:
fetch('/api/assistant', { method: 'POST', body: JSON.stringify({ message, orion: window.orion.contextText() }) });
```

Skill files in [skills/](skills/) teach coding and research agents the CLI: `orion-context`, `orion-research`,
`orion-datasets`, `orion-train` and `orion-deploy` (`npx skills add ryanlin10/posthog-behavioral-recorder`).

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
npm run test:python           # builder, labellers, export parity, end-to-end on generated data
```

The browser computes the same window features and time steps as the Python builder, and runs exported models
with the same outputs as scikit-learn and PyTorch; tests check both on generated data.

Independent project; not affiliated with PostHog. MIT licence.
