---
name: orion-datasets
description: Write and run Orion recipes that turn PostHog input telemetry into versioned, labelled training datasets, using AI labellers (text people wrote, session replay activity) and a human labelling page. Use when asked to build or change a dataset for a sentiment or behaviour classifier.
---

# Orion datasets

## Setup (once per project folder)

```sh
pip install paradigm-orion            # Python 3.11+
orion init --host https://us.posthog.com --project <project id>
export POSTHOG_PERSONAL_API_KEY=...   # scopes: query:read, batch_export:read, batch_export:write
export ANTHROPIC_API_KEY=...          # only for the AI labellers
```

The site must run Orion's browser script on top of PostHog, so `orion_input` events exist.

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
  by: [human, model]              # sources the dataset accepts (also self_report)
  min_confidence: 0.6
  model:
    reads: [text, replay]
    llm: claude-sonnet-5-5
    guide: |                      # the team's definitions, shown to the AI labellers
      Frustrated: repeated failed attempts, complaints, giving up. A calm bug report is not frustration.
    text: {event: $ai_generation, property: $ai_input}   # or {file: messages.jsonl} with session_id, timestamp, text
split:
  by: [user]                      # user (new people), day (later days held out), session, or [user, day]
  train: 0.7
  validation: 0.15
  test: 0.15
model:
  architecture: boosted_trees     # logistic, random_forest, boosted_trees, gru, tcn, transformer
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
orion build recipes/checkout-sentiment.yaml                       # → .orion/datasets/<name>/v<N>/
```

AI answers are cached in `.orion/cache/llm.jsonl`; `--relabel` asks again. Labels that overlap are resolved:
human beats text beats replay.

## Read the result

- `manifest.json`: counts per split and class, rejections by reason, warnings, checksums.
- `rejected.jsonl`: why windows were dropped (capture gaps, too few events, label policy).
- `orion.json`: the recipe used and the labeller statistics.

Check class balance per split and the rejection reasons before training. The replay labeller sees clicks that
the classifier also measures. Keep some human or text labels for validation when you can.
