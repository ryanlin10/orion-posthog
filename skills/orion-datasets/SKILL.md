---
name: orion-datasets
description: Write and run Orion recipes that turn PostHog input telemetry into versioned, labelled training datasets, using people's own answers (self-report), AI labellers (text people wrote, session replay activity) and a human labelling page. Use when asked to build or change a dataset for a sentiment or behaviour classifier.
---

# Orion datasets

## Setup (once per project folder)

```sh
pip install "paradigm-orion[sequence] @ git+https://github.com/ryanlin10/posthog-behavioral-recorder"   # Python 3.11+
orion init     # asks for the PostHog key and project, the AI labeller (Anthropic, OpenAI or Gemini) and its key,
               # and a TypeSafe key (Jev ranks the self-report answers); keys go to ~/.config/orion/keys.env
```

`orion init` needs a terminal; an agent can run `orion init --no-input --host <host> --project <id> --llm <provider or
model>` and leave keys to environment variables (POSTHOG_PERSONAL_API_KEY with query:read, project:read,
batch_export:read and batch_export:write; ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY; TYPESAFE_API_KEY).

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
  sentiment: [calm, engaged, confused, frustrated]   # or `frustrated: binary`, or `frustration: score(0, 1)`
window:
  length: 10s                     # 1s to 5m
  align: past                     # text labels: past (while writing), symmetric, or future
  stride: 10s                     # spacing of replay/human windows; at least length
telemetry: [pointer, keyboard, scroll, focus]   # add volume only on purpose: counts track typing amount
labels:
  by: [self_report, human, model] # people's own answers, the labelling page, AI labellers
  min_confidence: 0.6
  self_report:                    # optional; defaults to the target's classes when they include 2+ positive and 2+ negative
    question: How is it going so far?
    positive: [calm, engaged]     # the question offers two of each, ranked by `orion survey`
    negative: [confused, frustrated]
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
features: {exclude: []}           # e.g. key_interval_ms_median for an ablation
deploy: {context: [support-agent], every: 2s}
```

## Run

```sh
orion pull --since 30d --recipe recipes/checkout-sentiment.yaml   # PostHog file exports, one week at a time
orion label text recipes/checkout-sentiment.yaml                  # optional; build runs missing AI labellers
orion label replay recipes/checkout-sentiment.yaml
orion label human recipes/checkout-sentiment.yaml                 # local page; AI-rated windows come first
orion label self_report recipes/checkout-sentiment.yaml           # optional; build reads the answers itself
orion build recipes/checkout-sentiment.yaml                       # → .orion/datasets/<name>/v<N>/
```

AI answers are cached in `.orion/cache/llm.jsonl`; `--relabel` asks again. Labels that overlap are resolved:
self-report beats human beats text beats replay. A self-report answer labels the window that ended when the
question appeared; answers outside a categorical target's classes are skipped (counted as `not_this_target`).

## Read the result

- `manifest.json`: counts per split and class, rejections by reason, warnings, checksums.
- `rejected.jsonl`: why windows were dropped (capture gaps, too few events, label policy).
- `orion.json`: the recipe used and the labeller statistics.

Check class balance per split and the rejection reasons before training. The replay labeller sees clicks that
the classifier also measures. Keep some human or text labels for validation when you can.
