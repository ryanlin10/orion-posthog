"""Build Orion's base model (src/base-model.js) from the DUX dataset, and report how well it does on held-out people.

DUX (Bieber, Leppich, Harms and Schubert 2023, doi 10.5281/zenodo.7778612, CC BY 4.0): 46 people filled in a travel
expense web form with frustration triggers; every browser event was logged, and observers annotated short episodes of
emotion. A 10-second window is "negative" when at least a second of annotated anger, confusion, contempt, disgust,
judgement, fear or sadness falls in it, "neutral" when nothing is annotated within 5 s of it, and is left out otherwise.
Events become Orion records (keys paired, then forgotten, as the timing mask does), so the features are the ones
orion.js computes live: typing rhythm and pointer movement shape, without activity counts.

The model is logistic regression without class weights, so its probabilities match DUX's rate of negative windows,
and it labels a window "negative" from a probability of 0.25 (about twice that rate).

usage: python scripts/base_model.py [--cache ~/.cache/orion/dux]   (downloads 320 MB once)
"""
import argparse
import csv
import hashlib
import json
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from paradigm_orion.dataset.features import extract_features
from paradigm_orion.export import export_model
from paradigm_orion.recipe import compile_recipe

ROOT = Path(__file__).resolve().parents[1]
FILES = {"v0.csv": "2d316cf298de3ae9bcab5ad2c0a20f78", "v1.csv": "e40d60793de642ce0288d08ccdae4033"}
URL = "https://zenodo.org/api/records/7778612/files/{}/content"
NEGATIVE = {"Anger", "Confusion", "Contempt", "Disgust", "Judgement", "Fear", "Sadness"}
COUNTS = ("motion_moves", "typing_pause_count")  # How much someone did, not how: left out on purpose.
WINDOW_MS, MARGIN_MS, MIN_EVENTS, FLAG = 10000, 5000, 20, 0.25
RECIPE = {"name": "orion-base", "target": {"affect": ["neutral", "negative"]}, "window": {"length": "10s", "align": "past"},
          "telemetry": ["typing", "motion"], "features": {"exclude": list(COUNTS)}, "model": {"architecture": "logistic"},
          "quality": {"min_events": MIN_EVENTS}}


def download(cache):
    cache.mkdir(parents=True, exist_ok=True)
    for name, md5 in FILES.items():
        path = cache / name
        if not path.exists() or hashlib.md5(path.read_bytes()).hexdigest() != md5:
            print(f"Downloading DUX {name}")
            urllib.request.urlretrieve(URL.format(name), path)
            if hashlib.md5(path.read_bytes()).hexdigest() != md5:
                raise SystemExit(f"{name} does not match Zenodo's checksum")
    return [cache / name for name in FILES]


def sessions(paths):
    """{person: [(timestamp, row)]}; v0 and v1 hold different people, numbered per file."""
    out = defaultdict(list)
    for index, path in enumerate(paths):
        reader = csv.reader(path.open(newline=""), delimiter="\t")
        header = next(reader)
        for row in reader:
            d = dict(zip(header, row))
            out[f"v{index}-{d['session']}"].append((int(d["timestamp"]), d))
    return out


def convert(events):
    """Orion records, annotated emotion episodes and the element each event acted on."""
    events.sort(key=lambda e: e[0])
    origin = events[0][0]
    records, episodes, places, keys, open_, press = [], [], [], {}, {}, 0
    for ts, d in events:
        t, kind = float(ts - origin), d["type"]
        if kind != "MouseMovement":
            places.append((t, d["target"].split("<-Document")[0]))
        if kind == "MouseMovement" and d["x"] and d["y"]:
            records.append({"kind": "pointermove", "t": t, "x": float(d["x"]), "y": float(d["y"]), "pointer_id": 1})
        elif kind in ("MouseButtonDown", "MouseButtonUp", "MouseClick") and d["x"] and d["y"]:
            records.append({"kind": {"MouseButtonDown": "pointerdown", "MouseButtonUp": "pointerup", "MouseClick": "click"}[kind], "t": t,
                            "x": float(d["x"]), "y": float(d["y"]), "pointer_id": 1, "button": 0})
        elif kind == "KeyPressed":
            repeat = d["repeat"].lower() == "true"
            if not repeat:
                press += 1
                keys[d["key"]] = press
            records.append({"kind": "keydown", "t": t, "repeat": repeat, **({"press_id": keys[d["key"]]} if d["key"] in keys else {})})
        elif kind == "KeyReleased":
            records.append({"kind": "keyup", "t": t, "repeat": False, **({"press_id": keys.pop(d["key"])} if d["key"] in keys else {})})
        elif kind == "Scroll":
            records.append({"kind": "scroll", "t": t, "x": float(d["xPosition"] or 0), "y": float(d["yPosition"] or 0)})
        active = {k[15:] for k, v in d.items() if k.startswith("emotion_manual") and v not in ("", "0.0", "0")}
        for name in active - set(open_):
            open_[name] = t
        for name in set(open_) - active:
            episodes.append((name, open_.pop(name), t))
    records.sort(key=lambda r: r["t"])
    episodes += [(name, start, records[-1]["t"]) for name, start in open_.items()]
    return records, episodes, places


def windows(paths):
    out = []
    for person, events in sorted(sessions(paths).items()):
        records, episodes, places = convert(events)
        for a in range(0, int(records[-1]["t"]) - WINDOW_MS + 1, WINDOW_MS):
            b = a + WINDOW_MS
            negative = sum(max(0, min(e, b) - max(s, a)) for name, s, e in episodes if name in NEGATIVE)
            near = any(s < b + MARGIN_MS and e > a - MARGIN_MS for _, s, e in episodes)
            label = "negative" if negative >= 1000 else None if near else "neutral"
            inside = [r for r in records if a <= r["t"] < b]
            if label is None or len(inside) < MIN_EVENTS:
                continue
            here = [p for t, p in places if a <= t < b]
            out.append({"person": person, "label": label, "step": max(set(here), key=here.count) if here else "",
                        "events": [{**r, "t": r["t"] - a} for r in inside],
                        "features": extract_features(inside, {"start_ms": a, "end_ms": b}, RECIPE["telemetry"])[0]})
    return out


def pipeline():
    return make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True, add_indicator=True), StandardScaler(),
                         LogisticRegression(C=0.05, max_iter=3000))


def oof(x, y, groups, shuffle=None):
    p = np.zeros(len(y))
    for train, test in GroupKFold(n_splits=5).split(x, y, groups):
        labels = y[train] if shuffle is None else shuffle.permutation(y[train])
        model = pipeline().fit(x[train], labels)
        p[test] = model.predict_proba(x[test])[:, list(model.classes_).index("negative")]
    return p


def main():
    a = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    a.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "orion" / "dux")
    a.add_argument("--shuffles", type=int, default=19)
    args = a.parse_args()
    rows = windows(download(args.cache))
    names = sorted(n for n in rows[0]["features"] if n not in COUNTS)
    x = np.array([[np.nan if r["features"][n] is None else r["features"][n] for n in names] for r in rows], dtype=float)
    y = np.array([r["label"] for r in rows], dtype=object)
    groups = np.array([r["person"] for r in rows])
    truth = y == "negative"

    # Held-out people: five folds that never share a person.
    p = oof(x, y, groups)
    auc = roc_auc_score(truth, p)
    within = defaultdict(list)
    for r, pi, ti in zip(rows, p, truth):
        within[r["step"]].append((pi, ti))
    pairs = [(roc_auc_score([t for _, t in v], [q for q, _ in v]), min(sum(t for _, t in v), sum(not t for _, t in v)))
             for v in within.values() if 0 < sum(t for _, t in v) < len(v)]
    within_auc = float(np.average([s for s, _ in pairs], weights=[w for _, w in pairs]))
    rng = np.random.default_rng(1)
    null = [roc_auc_score(truth, oof(x, y, groups, rng)) for _ in range(args.shuffles)]
    flagged = p >= FLAG
    order = np.argsort(p)
    report = {"windows": len(rows), "people": len(set(groups)), "negative_rate": round(float(truth.mean()), 3), "auc": round(auc, 3),
              "auc_same_form_step": round(within_auc, 3),
              "shuffled_auc_median": round(float(np.median(null)), 3), "shuffled_at_least": int(sum(n >= auc for n in null)),
              "p_value": round((1 + sum(n >= auc for n in null)) / (1 + len(null)), 3),
              "flag": {"min_probability": FLAG, "flags_share": round(float(flagged.mean()), 3),
                       "right_share": round(float(truth[flagged].mean()), 3), "finds_share": round(float(flagged[truth].mean()), 3)},
              "calibration": [[round(float(p[b].mean()), 3), round(float(truth[b].mean()), 3)] for b in np.array_split(order, 5)]}
    print(json.dumps(report, indent=2))

    model = pipeline().fit(x, y)
    recipe = compile_recipe(RECIPE)
    spec = export_model(model, names, x, recipe, 1, {
        "summary": f"Held-out people: AUC {report['auc']:.2f} ({report['auc_same_form_step']:.2f} within the same form step); "
                   f"flags {report['flag']['flags_share']:.0%} of windows, right {report['flag']['right_share']:.0%} of the time "
                   f"against {report['negative_rate']:.0%} by chance.", "validation": report})
    spec.pop("live", None)
    spec["decision"] = {"class": "negative", "min_probability": FLAG}
    spec["about"] = {
        "what": "Signs of negative feeling (mostly confusion, some anger) in the last 10 seconds of typing rhythm and pointer movement shape.",
        "trained_on": "DUX: 46 people filling in a travel expense web form with frustration triggers; observers annotated emotion episodes. "
                      "Bieber, Leppich, Harms and Schubert 2023, doi 10.5281/zenodo.7778612, CC BY 4.0.",
        "limits": "Weak: when it says negative it is right about a quarter of the time. Probabilities match DUX's rate of negative windows, "
                  "not your site's. It did not predict self-reported mood in everyday computer use (Freihaut and Göritz field study, "
                  "174 people: AUC 0.50). Use it to choose replays to watch or when to ask, not to change what an assistant says.",
    }
    (ROOT / "src" / "base-model.js").write_text(
        "// Generated by scripts/base_model.py from the DUX dataset (CC BY 4.0, see NOTICE). Do not edit.\n"
        f"export const BASE_MODEL = {json.dumps(spec, separators=(',', ':'))};\n")
    # A few real windows with scikit-learn's probabilities, so the tests check that the browser computes the same.
    picks, seen = [], set()
    for r in sorted(rows, key=lambda r: (r["label"], len(r["events"]), r["person"])):
        if r["person"] not in seen and 30 <= len(r["events"]) <= 250 and sum(q["label"] == r["label"] for q in picks) < 4:
            picks.append(r)
            seen.add(r["person"])
    fixture = [{"label": r["label"], "events": r["events"],
                "probability": float(model.predict_proba(np.array([[np.nan if r["features"][n] is None else r["features"][n] for n in names]]))[0, list(model.classes_).index("negative")])}
               for r in picks]
    (ROOT / "test" / "fixtures" / "base-model-windows.json").write_text(json.dumps(fixture, separators=(",", ":")))
    print(f"Wrote src/base-model.js ({len(names)} features) and {len(fixture)} test windows")


if __name__ == "__main__":
    main()
