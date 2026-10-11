# Build training data from telemetry

This tool structures recorded behavior and explicit annotations into reproducible datasets. It does not infer training labels from typing speed, equate absent labels with neutral sentiment, or claim that telemetry can predict any particular emotion.

The builder requires Python 3.10+ and uses only the standard library. Run from a checkout, or install the Python package locally:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
# Then use .venv/bin/telemetry-dataset instead of python3 -m telemetry_dataset.
```

## Three inputs and a recipe

1. **Telemetry JSONL:** one `behavioral_burst` properties object or PostHog event envelope per line. Schema 1.0, 1.1 and 1.2 are supported, including columnar records. Other PostHog event names are counted and skipped. Arrays/CSV/rrweb snapshots are not this input format; convert them explicitly first.
2. **Recording metadata JSONL:** map each recorder to its experimental split group. Use a stable participant ID to evaluate new-person generalization. Never assign each overlapping window its own group.
3. **Interval annotations JSONL:** explicit target values, evidence source, annotation version, confidence and temporal scope.
4. **Recipe JSON:** target definitions, windowing, quality policies, feature selection, split assignments and export options.

```json
{"recorder_id":"recording-1","group_id":"participant-1","subject_id":"participant-1","session_id":"session-1"}
```

Example annotation:

```json
{"id":"annotation-1","recorder_id":"recording-1","timebase":"recorder_ms","start_ms":10000,"end_ms":20000,"source":"self_report","version":"survey-v1","confidence":1.0,"targets":{"sentiment":"negative"}}
```

`start_ms` and `end_ms` use the recorder's relative `t` clock, **not epoch milliseconds or replay-player offsets**. Intervals are half-open: `[start_ms, end_ms)`. Convert external annotation clocks explicitly, using the recorded clock anchors and respecting discontinuities. No label is extended into adjacent unlabeled time.

`source` is `human`, `self_report`, `model`, `heuristic`, or `synthetic`. Model/heuristic labels require explicit recipe opt-in and remain marked as proxies in every output example. `version` identifies the annotation protocol/prompt; `confidence` is supplied by that protocol, not assumed to be a calibrated probability. Label text and rationales are not exported to model features.

```sh
python3 -m telemetry_dataset \
  --recipe recipes/sentiment.json \
  --batches /path/to/events-1.jsonl /path/to/events-2.jsonl \
  --recordings /path/to/recordings.jsonl \
  --labels /path/to/labels.jsonl \
  --out datasets/sentiment-v1
```

Output must be a new directory. A failed build publishes no partial dataset. All data processing is local; no API keys or paid model calls are involved.

## Customize the recipe

Start with [sentiment.json](../recipes/sentiment.json), [multitask.json](../recipes/multitask.json), or [implicit.json](../recipes/implicit.json). The class names are examples; there is no hard-coded mood taxonomy.

| Option | Behavior |
| --- | --- |
| `targets.<name>.type` | `categorical`, `multilabel`, or `continuous`; define any number of targets |
| `classes` | Allowed string classes for categorical/multilabel targets |
| `min`, `max` | Required finite bounds for continuous targets |
| `window.mode` | `label`: one example per annotation; `sliding`: complete windows inside each annotation |
| `window.size_ms`, `stride_ms` | Sliding-window size/stride; overlap stays within the same split group |
| `window.max_events` | Default 100,000; larger windows are rejected, not truncated or resampled |
| `window.max_duration_ms` | Default 300,000; limits each generated example |
| `window.max_windows_per_label` | Default 10,000; guards accidental runaway annotation expansion |
| `labels.sources`, `min_confidence` | Allowed annotation sources and minimum supplied confidence |
| `quality.min_events` | Default 2 actual input records, excluding boundary markers |
| `quality.allow_gaps` | Default false; detects sequence gaps, boundaries, reported losses and clock-offset jumps over 100 ms |
| `quality.allow_synthetic` | Default false; false `trusted` or a synthetic-enabled recorder marks synthetic input |
| `quality.require_quality_metadata` | Default false for older schemas; unknown quality is always reported |
| `features.families` | Any of `keyboard`, `pointer`, `scroll`, `volume`; volume is off by default |
| `features.exclude` | Remove specific features, e.g. pace measures for an ablation; misspelled names fail |
| `features.hook` | Relative path to a trusted Python feature extractor |
| `split.seed`, `fractions` | Deterministic hash assignment of groups; default 70/15/15; no row shuffling |
| `split.assignments` | Optional `{ "group-id": "train/validation/test" }` overrides, e.g. held-out days |
| `split.allow_subject_overlap` | Default false; explicit true enables within-person/day studies, with a warning |
| `output.include_sequence` | Default false; include sanitized, full-resolution samples for sequence models |

Unknown recipe fields fail instead of silently using defaults. A requested target missing from a label is an error. Overlapping *eligible* annotations must be adjudicated upstream; a filtered-out proxy annotation may coexist with a human annotation.

For a one-person temporal study, group all recordings from each day together, set `allow_subject_overlap: true`, and explicitly assign earlier days to train and later days to validation/test. This evaluates new periods for that person, not generalization to other people. Sessions must never cross groups; merge day groups if one session spans them. Grouping correctness ultimately depends on the metadata supplied.

### Feature hooks

```python
def extract(events, context):
    # events: sanitized records in timestamp order, within this window only
    # context: start_ms and end_ms; no target, identity or annotation content
    return {"my_feature": 1.0}  # finite numbers or None; same keys every time
```

See [custom_features.py](../examples/training/custom_features.py). Hooks are executable local Python, not a sandbox. Keep them pure and stateless; do not open annotation files, fit a baseline across splits, or use future windows. The builder hashes the hook and requires a fixed feature schema. Learned normalization belongs in the trainer, fitted only on train.

Built-in keyboard features use timing, overlapping presses and pauses. Pointer features include down/up hold durations, intervals between presses, movement cadence and speed; cancellations clear held buttons. They never use physical key codes, modifier identities, punctuation or reconstructed text. Legacy key codes are used locally only to associate a down with an up, then removed. Optional sequences also exclude codes, text, DOM targets, URLs, SDK identity enrichment and modifier/button identities. Each press receives a fresh opaque pairing ID. These omissions reduce content exposure; behavioral data can still identify a person.

Missing measurements remain `null`, not zero. A lack of pointer movement does not create a zero-speed observation. Long pointer gaps (100 ms or more) are excluded from active-motion speed calculations. Presses or releases outside a window remain unmatched; no duration is fabricated across a gap.

## Output contract

- `train.jsonl`, `validation.jsonl`, `test.jsonl`: each example has `features`, `targets`, temporal bounds, group/recorder IDs, `label` provenance, and `quality`. Optional `sequence.t` is relative to the example start; all samples are retained.
- `rejected.jsonl`: example/annotation ID, bounds and rejection reason. No raw telemetry.
- `manifest.json`: resolved recipe, feature order, group assignments, source and builder SHA-256 hashes, hook hash, output checksums, class/split counts, rejection totals and warnings.

**Model inputs are only `features` or the intended sequence representation.** IDs, label metadata, split membership and quality fields are audit information. Do not automatically feed every field to a model. A unique opaque press ID is for matching events, not a numerical predictor.

Batch retries are deduplicated by recorder/sequence. Conflicting retries fail. SQLite stages out-of-order files and indexes windows on disk; individual input lines and sample windows are bounded. Annotation/recording metadata stays in memory. This is designed for large telemetry streams, not unlimited annotations or arbitrary-sized single JSON values.

Quality metadata describes known evidence, not proof of complete capture. Missing terminal batches, unobserved browser drops, missing trust fields and server rejection after SDK acceptance can remain unknown. Clock anchors cannot repair data spanning an unobserved clock jump. Empty/missing classes and empty splits are reported; the builder never mixes groups to make a prettier balance.

## Runnable example and training

```sh
python3 examples/training/make_demo.py
python3 -m telemetry_dataset \
  --recipe artifacts/training-demo-input/recipe.json \
  --batches artifacts/training-demo-input/batches.jsonl \
  --labels artifacts/training-demo-input/labels.jsonl \
  --recordings artifacts/training-demo-input/recordings.jsonl \
  --out artifacts/training-demo-output
```

The demo explicitly opts into synthetic labels and input. Its labels are unrelated to its telemetry: it tests the plumbing, not emotion prediction.

An optional small-dataset reference trainer accepts its own JSON recipe. It requires Python 3.11+ for the pinned scikit-learn version; use a 3.11+ virtual environment for the following commands:

```sh
.venv/bin/python -m pip install '.[training]'
.venv/bin/python examples/training/train_classifier.py \
  --dataset artifacts/training-demo-output \
  --recipe examples/training/logistic.json \
  --out artifacts/training-demo-model
```

Change `target`, `estimator` (`logistic` or `random_forest`) and estimator `parameters`. It checks dataset hashes/group separation, fits imputation/scaling on train only, and reports validation performance alongside a class-prior baseline. Test evaluation requires `--evaluate-test`, after the recipe is selected. The output is `classifier.joblib` plus `report.json`. The builder remains model-independent: use the JSONL files with PyTorch, another tabular learner, or your own trainer for continuous/multilabel targets. The example trainer loads its small dataset in memory; it is not the streaming component.

## Evaluation practices behind the design

- Text/model sentiment annotations are proxies. Keep separate human/self-report evaluation sets.
- A typing-volume baseline can explain an apparent sentiment effect. Compare the default recipe with volume-only and pace-excluded ablations.
- Splitting adjacent or overlapping windows randomly can inflate performance. Hold out entire participants, sessions or explicitly selected periods.
- Punctuation can leak through key identities even when typed text is absent. Pair timing locally and omit key identities.
- Labels must describe the measured interval. Do not inherit a typed-message label into surrounding mouse-only activity without independent evidence.
- Evaluate temporal/domain shifts, class support, calibration, and grouped or temporal null tests before claiming useful prediction. Dataset construction does not itself establish that signal exists.

The reference trainer follows [scikit-learn's train-only preprocessing guidance](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage). No personal data or results from local research projects are included in this repository.
