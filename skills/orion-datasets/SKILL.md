---
name: orion-datasets
description: Write and run Orion recipes that turn PostHog input telemetry into versioned, labelled training datasets, using people's own answers (self-report), goal events the site already tracks (outcomes such as purchase or cancel), AI labellers (text people wrote, session replay activity) and a human labelling page. Use when asked to build or change a dataset for a sentiment, behaviour or outcome classifier.
---

# Orion datasets

## Setup (once per project folder)

```sh
pip install "paradigm-orion[sequence] @ git+https://github.com/ryanlin10/orion-posthog"   # Python 3.11+
orion init     # sign in to PostHog in the browser (the project is chosen there), the AI labeller and its key,
               # and an optional TypeSafe key (Jev ranks the self-report answers); each is checked live
orion check    # what works, and what fixes the rest
```

Run them in the app's folder: Orion keeps `.orion/` beside the app and finds it from subfolders, and refuses the home
folder. In a terminal `orion init` asks there; from an agent's shell it opens a setup page in the person's browser,
so keys never pass through the agent. The PostHog sign-in carries exactly the scopes `orion pull` needs. Keys and
sign-ins live outside the project (`~/.config/orion/`, readable only by you). For CI, environment variables still
work: POSTHOG_PERSONAL_API_KEY (scopes query:read, project:read, batch_export:read and batch_export:write),
ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY, and TYPESAFE_API_KEY.

The site must run Orion's browser script on top of PostHog, so `orion_input` events exist. For self-report labels it
also needs `selfReport` on (see orion-deploy).

## Recipe

`recipes/<name>.yaml`:

```yaml
name: checkout-sentiment          # lowercase and dashes; names datasets and models
source:
  since: 30d                      # how far back to use pulled data
  pages: [/checkout]              # optional path prefixes
target:
  feeling: [calm, pleased, engaged, confident, frustrated, confused, anxious, angry, bored]
  # all nine of the question's answers (the default), or fewer classes, `frustrated: binary`, `frustration: score(0, 1)`
window:
  length: 10s                     # 1s to 5m
  align: past                     # text labels: past (while writing), symmetric, or future
  stride: 10s                     # spacing of replay/human windows; at least length
telemetry: [pointer, keyboard, scroll, focus, fills, visibility, depth, overshoot]
  # volume only on purpose (counts track typing amount); corrections needs mask: 'types' on the site
labels:
  by: [self_report, human, model] # people's own answers, the labelling page, AI labellers
  min_confidence: 0.6
  self_report:                    # optional: the question and the answers it chooses from, two of each per visitor
    question: How is it going so far?
    positive: [calm, pleased, engaged, confident]       # default: the target's classes when 2+ of each are known
    negative: [frustrated, confused, anxious, angry, bored]   # feelings; a target of four leaves nothing to choose
  model:
    reads: [text, replay]
    llm: claude-sonnet-5-5        # optional; default is the model chosen at `orion init` (gpt-5.6-sol, gemini-3.8-flash…)
    guide: |                      # the team's definitions, shown to the AI labellers
      Frustrated: repeated failed attempts, complaints, giving up. A calm bug report is not frustration.
    text: {event: $ai_generation, property: $ai_input}   # or {file: messages.jsonl} with session_id, timestamp, text
split:
  by: [user]                      # user (new people), day (later days held out), session, or [user, day]; session with user or day adds nothing
  train: 0.7
  validation: 0.15
  test: 0.15
model:
  architecture: boosted_trees     # logistic, random_forest, boosted_trees, gru, tcn (or temporal_cnn), transformer
  trees: 200                      # optional settings: trees, depth / layers, heads, hidden, kernel, epochs
quality: {drop_gaps: true, drop_synthetic: true, min_events: 2}
features:
  exclude: []                     # e.g. key_interval_ms_median for an ablation
  baseline: true                  # adds <feature>_rel: each window against the same browser's earlier page loads
deploy: {context: [support-agent], every: 2s}
```

## Telemetry families

| Family | Features | Needs |
| --- | --- | --- |
| pointer | movement cadence and speed, click holds, time between clicks | |
| keyboard | key holds, time between presses, overlap, pauses | |
| scroll, focus | time between scroll steps; time in each field | |
| volume | counts: key presses, pointer travel, events | on purpose only |
| fills | `paste_count`, `autofill_count`: fields filled without typing (never the value) | orion.js 0.4 |
| visibility | `hidden_count`, `hidden_ms`: leaving the tab and for how long; keeps windows with time away | orion.js 0.4 |
| depth | `scroll_depth_max`: share of the page scrolled into view | orion.js 0.4 |
| overshoot | `overshoot_fraction`, `overshoot_px_median`: presses where the pointer went past the control and came back | orion.js 0.4 |
| corrections | `correction_rate`: deletes per key press | `mask: 'types'` on the site; null with the default timing mask |
| typing | press-to-press, release-to-press and hold quartiles; pause count, length and share of typing time | |
| motion | movement shape: directness, deviation, crossings, direction consistency, turning, speed profile, jitter | |
| touch | contact area and real pressure on phones and pens | orion.js 0.4 |

Data recorded before orion.js 0.4 has none of the 0.4 signals: those features stay at zero or null there, so pull
fresh data before relying on them. `features.baseline` works with logistic, random_forest and boosted_trees models;
it compares each window with the same browser's earlier page loads (PostHog's `$device_id`), and orion.js keeps the
matching averages in the visitor's browser. A first visit has no baseline (`_rel` is null).

## Outcome labels from goal events

Orion records no goals itself; it reads the events the site already sends to PostHog. Check the code for
`posthog.capture('<goal>')` calls or a confirmation page before you write this. Without goal events, say so and
suggest which to add; do not invent them.

```yaml
target:
  outcome: [purchased, left]      # or `converted: binary` with events {yes: [...]} and otherwise: no
labels:
  by: [outcome]
  outcome:
    events:
      purchased: [order completed, /checkout/done]   # event names, or page paths (page views) starting with /
    otherwise: left               # sessions with none of them, once they have ended; omit to use goal sessions only
    within: 5m                    # how far before the goal the windows reach
    skip: 5s                      # leave out the last seconds before the goal (the click that completes it)
    windows: 6                    # most recent windows per session
```

`orion pull --recipe` also pulls the goal events. The first goal in a session decides its class. Outcome labels say
what a session went on to do, not how anyone felt: describe the model that way to people and agents.

## Run

```sh
orion pull --since 30d --recipe recipes/checkout-sentiment.yaml   # PostHog file exports, one week at a time
orion label text recipes/checkout-sentiment.yaml                  # optional; build runs missing AI labellers
orion label replay recipes/checkout-sentiment.yaml
orion label human recipes/checkout-sentiment.yaml                 # local page; AI-rated windows come first
orion label self_report recipes/checkout-sentiment.yaml           # optional; build reads the answers itself
orion label outcome recipes/checkout-outcome.yaml                 # optional; build reads the goal events itself
orion build recipes/checkout-sentiment.yaml                       # → .orion/datasets/<name>/v<N>/
```

`orion train` does the pull, labelling and build itself; run these steps on their own to inspect a dataset first.

AI answers are cached in `.orion/cache/llm.jsonl`; `--relabel` asks again. Labels that overlap are resolved:
self-report beats human beats outcome beats text beats replay. A self-report answer labels the window that ended when the
question appeared; answers outside a categorical target's classes are skipped (counted as `not_this_target`).

## Read the result

- `manifest.json`: counts per split and class, rejections by reason, warnings, checksums.
- `rejected.jsonl`: why windows were dropped (capture gaps, too few events, label policy).
- `orion.json`: the recipe used and the labeller statistics.

Check class balance per split and the rejection reasons before training. The replay labeller sees clicks that
the classifier also measures. Keep some human or text labels for validation when you can.
