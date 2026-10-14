# The dataset builder

`orion build` and `orion train` call this builder for you (see the [README](../README.md)). This page documents
the builder itself, for using it directly with your own labels and recording metadata.

This tool structures recorded behavior and explicit annotations into reproducible datasets. It does not infer training labels from typing speed, equate absent labels with neutral sentiment, or claim that telemetry can predict any particular emotion.

The builder requires Python 3.10+ and uses only the standard library. Run from a checkout, or install the Python package locally:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
# Then use .venv/bin/orion-dataset instead of python3 -m paradigm_orion.dataset.
```

## Three inputs and a recipe

1. **Telemetry JSONL:** one `orion_input` (or older `behavioral_burst`) properties object or PostHog event envelope per line. Schema 1.0 to 1.4 are supported, including columnar records. Other PostHog event names are counted and skipped. Arrays/CSV/rrweb snapshots are not this input format; convert them explicitly first.
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

`source` is `human`, `self_report`, `outcome`, `model`, `heuristic`, or `synthetic`. Model/heuristic labels require explicit recipe opt-in and remain marked as proxies in every output example. `version` identifies the annotation protocol/prompt; `confidence` is supplied by that protocol, not assumed to be a calibrated probability. Label text and rationales are not exported to model features.

A minimal recipe:

```json
{"version":1,"targets":{"feeling":{"type":"categorical","classes":["calm","frustrated"]}},"labels":{"sources":["human","self_report"]}}
```

```sh
python3 -m paradigm_orion.dataset \
  --recipe recipe.json \
  --batches /path/to/events-1.jsonl /path/to/events-2.jsonl \
  --recordings /path/to/recordings.jsonl \
  --labels /path/to/labels.jsonl \
  --out datasets/sentiment-v1
```

Output must be a new directory. A failed build publishes no partial dataset. All data processing is local; no API keys or paid model calls are involved.

## Customize the recipe

Everything not set takes the default below. The class names are examples; there is no hard-coded mood taxonomy.

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
| `labels.sources`, `min_confidence` | Allowed annotation sources (`human`, `self_report`, `outcome`, `model`, `heuristic`, `synthetic`) and minimum supplied confidence |
| `quality.min_events` | Default 2 actual input records, excluding boundary markers |
| `quality.allow_gaps` | Default false; detects sequence gaps, boundaries, reported losses and clock-offset jumps over 100 ms |
| `quality.allow_away` | Default false; true keeps windows whose only gaps are the person leaving (blur, hidden, pagehide). Orion recipes set it with the `visibility` family |
| `quality.allow_synthetic` | Default false; false `trusted` or a synthetic-enabled recorder marks synthetic input |
| `quality.require_quality_metadata` | Default false for older schemas; unknown quality is always reported |
| `features.families` | Any of `keyboard`, `pointer`, `scroll`, `focus`, `volume`, `fills`, `visibility`, `depth`, `overshoot`, `corrections`, `typing`, `motion`, `touch`; the default is keyboard, pointer and scroll |
| `features.baseline` | Default null; `{tile_ms, grace_ms, cap, min_tiles}` adds `<feature>_rel`, the window against the same person's earlier page loads (see below) |
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

Schema 1.4 families, each opt-in so existing recipes keep their features:

| Family | Features | Needs |
| --- | --- | --- |
| `fills` | `paste_count`, `autofill_count`: fields changed by paste or drop, and by autofill | schema 1.4 |
| `visibility` | `hidden_count`, `hidden_ms`: returns to the page, and the time hidden that falls inside the window | schema 1.4; keeps windows with time away |
| `depth` | `scroll_depth_max`: the deepest share of the page (or main scrolling area) in view, 0 to 1 | schema 1.4 |
| `overshoot` | `overshoot_fraction`, `overshoot_px_median`: of presses on a control with pointer movement in the second before, the share where the pointer reached the control, left it by 2 px or more and came back, and how far it went | schema 1.4 |
| `corrections` | `correction_rate`: deletes per key press | `mask: 'types'` (or `code`); null with the timing mask |
| `typing` | quartiles of press-to-press (`typing_dd_ms`), release-to-press (`typing_flight_ms`) and hold (`typing_hold_ms`) times within typing; pauses of a second or more (`typing_pause_count`, `_ms_mean`, `_ms_sd`, `typing_pause_time_fraction`) | quartiles need 4 values |
| `motion` | per pointer movement (samples under 100 ms apart, read at most every 40 ms, at least 5 samples and 20 px): directness, excess travel, deviation from the straight line relative to its length, midline crossings, direction consistency, turning per 100 px, mean and peak speed, where the peak falls, late against early speed, and jitter between neighbouring 100 ms steps; the window keeps medians, the peak-speed spread across movements and speed × consistency | movements cut at presses, scrolls and gaps |
| `touch` | contact area mean, maximum and spread, and pressure where the device reports it (not 0 or 0.5) | schema 1.4 touch or pen |

The `typing` and `motion` families follow the research summarised in [base-model.md](base-model.md): timing distributions rather than averages, path shape against the direct line, and short-scale instability. Reading the pointer at most every 40 ms keeps shapes comparable between a 60 Hz trackpad and a 1000 Hz mouse.

Per-person baselines (`features.baseline`) compare a window with how the same person usually behaves. Each recorder (page load) is cut into tiles of `tile_ms`, counted from its time origin; a tile counts once input arrives `grace_ms` after it ends, if it passes the window rules. Each person's tiles feed running means, in order of page load, capped at `cap` tiles so older behaviour fades. A window gets `<feature>_rel` = its value minus the mean from that person's *earlier* page loads, or null until the mean rests on `min_tiles` tiles. Recordings rows name the person with `baseline_key` (Orion uses PostHog's `$device_id`, the browser), else `subject_id`. In the browser, `src/baseline.js` keeps the same means in `localStorage` (`orion-baseline:<model>`: averages of window features, never input) while capture is permitted, so live scoring matches. Means are features only; labels never enter them, so they cannot leak targets across splits.

Missing measurements remain `null`, not zero. A lack of pointer movement does not create a zero-speed observation. Long pointer gaps (100 ms or more) are excluded from active-motion speed calculations. Presses or releases outside a window remain unmatched; no duration is fabricated across a gap.

## Output contract

- `train.jsonl`, `validation.jsonl`, `test.jsonl`: each example has `features`, `targets`, temporal bounds, group/recorder IDs, `label` provenance, and `quality`. Optional `sequence.t` is relative to the example start; all samples are retained.
- `rejected.jsonl`: example/annotation ID, bounds and rejection reason. No raw telemetry.
- `manifest.json`: resolved recipe, feature order, group assignments, source and builder SHA-256 hashes, hook hash, output checksums, class/split counts, rejection totals and warnings.

**Model inputs are only `features` or the intended sequence representation.** IDs, label metadata, split membership and quality fields are audit information. Do not automatically feed every field to a model. A unique opaque press ID is for matching events, not a numerical predictor.

Batch retries are deduplicated by recorder/sequence. Conflicting retries fail. SQLite stages out-of-order files and indexes windows on disk; individual input lines and sample windows are bounded. Annotation/recording metadata stays in memory. This is designed for large telemetry streams, not unlimited annotations or arbitrary-sized single JSON values.

Quality metadata describes known evidence, not proof of complete capture. Missing terminal batches, unobserved browser drops, missing trust fields and server rejection after SDK acceptance can remain unknown. Clock anchors cannot repair data spanning an unobserved clock jump. Empty/missing classes and empty splits are reported; the builder never mixes groups to make a prettier balance.

## Training on the output

`orion train` trains, checks and exports models from datasets that `orion build` made. With your own labels and
metadata, train on the JSONL files with any learner: fit imputation and scaling on `train` only, choose settings on
`validation`, and score `test` once at the end. Continuous and multilabel targets are built here but `orion train` and
the browser runtime take one categorical or continuous target.

## Evaluation practices behind the design

- Text/model sentiment annotations are proxies. Keep separate human/self-report evaluation sets.
- A typing-volume baseline can explain an apparent sentiment effect. Compare the default recipe with volume-only and pace-excluded ablations.
- Splitting adjacent or overlapping windows randomly can inflate performance. Hold out entire participants, sessions or explicitly selected periods.
- Punctuation can leak through key identities even when typed text is absent. Pair timing locally and omit key identities.
- Labels must describe the measured interval. Do not inherit a typed-message label into surrounding mouse-only activity without independent evidence.
- Evaluate temporal/domain shifts, class support, calibration, and grouped or temporal null tests before claiming useful prediction. Dataset construction does not itself establish that signal exists.

`orion train` follows [scikit-learn's train-only preprocessing guidance](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage). No personal data or results from local research projects are included in this repository.
