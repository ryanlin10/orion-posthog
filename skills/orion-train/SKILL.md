---
name: orion-train
description: Train and evaluate an Orion sentiment classifier from a recipe (boosted trees, random forest, logistic, GRU, temporal CNN or transformer), read its report honestly, and iterate. Use when asked to train a classifier on telemetry, compare architectures, or decide whether a model is good enough to deploy.
---

# Orion train

```sh
orion train recipes/checkout-sentiment.yaml                      # builds a fresh dataset version, then trains
orion train checkout-sentiment --arch transformer                # name of a recipe in recipes/, or of a built dataset
orion train checkout-sentiment --no-rebuild --arch gru           # reuse the latest dataset version
orion train checkout-sentiment --evaluate-test                   # score the test split: once, at the end
```

Each run writes `.orion/models/<name>/v<N>/`: `model.json` (what the browser runs), `report.json`, `recipe.json`,
and for sequence models an `.onnx` file. Sequence architectures need the `[sequence]` extra (see orion-datasets).
They train on CPU and are much slower than trees. They read 100 ms time steps with only the channels of the recipe's
telemetry families, so dropping a family really removes it (`report.json` lists the `channels`).

## Read the report

The summary line is the place to start, e.g.
`Validation: balanced accuracy 0.84 on 168 windows (chance 0.50). Shuffled-label models matched or beat it in 0 of 20 tries (p = 0.05).`

- **Validation is new people** (or later days) by default. A high score on people the model trained on means little.
- **Shuffled-label check.** The same model is retrained with the labels shuffled. If shuffled models often match
  the real one (p above 0.05), the model has learnt nothing usable. Say so plainly and do not deploy it.
- **Per-class recall** in `metrics.validation.recall`: a model that never predicts the rare class can still look fine on accuracy.
- **Label sources** in `label_sources`: proxy labels (model) are not the person's own report; `self_report` labels are.
- **Loss curve** in `curve`: training and validation loss per epoch (sequence models) or per round of trees
  (boosted trees). Validation loss rising while training loss falls means the model is memorising.

## Iterate

Change one thing per run and compare validation numbers:
- window length (5 to 30 seconds is typical) and alignment
- telemetry families, and `features.exclude` to test whether one signal (such as typing speed) carries everything
- labels: more human labels, a sharper `guide`, a higher `min_confidence`
- architecture: trees first; sequence models need more labelled windows (hundreds per class)

Use `--evaluate-test` only after choosing the recipe on validation, and report that number as the final one.
