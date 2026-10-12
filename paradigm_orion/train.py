"""Train a classifier on a built dataset, check it on held-out people, and save a versioned browser model.

Every model is compared with two references: a model that always predicts the most common answer from
training, and the same model trained again on shuffled labels. A model that cannot beat shuffled labels
has learnt nothing usable, whatever its headline score.
"""
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone

import joblib
import numpy as np
import sklearn
from scipy.stats import spearmanr
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor, RandomForestClassifier, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import balanced_accuracy_score, f1_score, log_loss, mean_absolute_error, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .export import export_model
from .recipe import SEQUENCE_ARCHITECTURES

SPLITS = ("train", "validation", "test")


def load_dataset(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    rows = {}
    for split in SPLITS:
        path = directory / f"{split}.jsonl"
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest["output_sha256"][path.name]:
            raise ValueError(f"{path} does not match its manifest checksum")
        rows[split] = [json.loads(line) for line in content.splitlines() if line]
    groups = [{r["group_id"] for r in rows[s]} for s in SPLITS]
    if groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]:
        raise ValueError("The same group appears in more than one split")
    return manifest, rows


def matrix(rows, names):
    return np.array([[np.nan if r["features"][n] is None else r["features"][n] for n in names] for r in rows], dtype=float).reshape(len(rows), len(names))


def estimator_for(architecture, target_type, params, seed):
    trees, depth = params.get("trees"), params.get("depth")
    if target_type == "continuous":
        if architecture == "logistic":
            return Ridge(alpha=float(params.get("alpha", 1.0)))
        if architecture == "random_forest":
            return RandomForestRegressor(n_estimators=int(trees or 300), max_depth=depth, min_samples_leaf=int(params.get("min_leaf", 2)), n_jobs=1, random_state=seed)
        return GradientBoostingRegressor(n_estimators=int(trees or 200), max_depth=int(depth or 3), learning_rate=float(params.get("learning_rate", 0.05)), subsample=0.8, random_state=seed)
    if architecture == "logistic":
        return LogisticRegression(C=float(params.get("c", 0.2)), max_iter=2000, class_weight="balanced")
    if architecture == "random_forest":
        return RandomForestClassifier(n_estimators=int(trees or 300), max_depth=depth, min_samples_leaf=int(params.get("min_leaf", 2)),
                                      class_weight="balanced", n_jobs=1, random_state=seed)
    return GradientBoostingClassifier(n_estimators=int(trees or 200), max_depth=int(depth or 3), learning_rate=float(params.get("learning_rate", 0.05)),
                                      subsample=0.8, random_state=seed)


def boosting_curve(model, target, x_train, y_train, x_val, y_val, points=100):
    """Train and validation loss after each round of trees, for boosted models (other estimators have no rounds)."""
    estimator = model.steps[-1][1]
    if not isinstance(estimator, (GradientBoostingClassifier, GradientBoostingRegressor)):
        return None
    prepare = model[:-1]
    continuous = target["type"] == "continuous"

    def losses(x, y):
        if not len(y):
            return None
        xt = prepare.transform(x)
        if continuous:
            return [float(np.mean((np.asarray(y, dtype=float) - p) ** 2)) for p in estimator.staged_predict(xt)]
        known = [i for i, v in enumerate(y) if v in set(estimator.classes_)]
        if not known:
            return None
        return [float(log_loss([y[i] for i in known], p[known], labels=estimator.classes_)) for p in estimator.staged_predict_proba(xt)]

    train, validation = losses(x_train, y_train), losses(x_val, y_val)
    every = max(1, len(train) // points)
    rounds = [i for i in range(len(train)) if i % every == every - 1 or i == len(train) - 1]
    return {"loss": "mean squared error" if continuous else "log loss", "unit": "trees",
            "epochs": [{"epoch": i + 1, "train": round(train[i], 5), "validation": None if validation is None else round(validation[i], 5)} for i in rounds]}


def pipeline_for(architecture, target_type, params, seed):
    return make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True, add_indicator=True), StandardScaler(),
                         estimator_for(architecture, target_type, params, seed))


def score(target, truth, model, x):
    """The headline number: balanced accuracy for classes, Spearman correlation for scores."""
    predicted = model.predict(x)
    if target["type"] == "continuous":
        rho = spearmanr(truth, predicted).statistic if len(set(truth)) > 1 and len(set(predicted)) > 1 else 0.0
        return float(0.0 if np.isnan(rho) else rho)
    return float(balanced_accuracy_score(truth, predicted))


def metrics(target, model, baseline, x, truth):
    if not len(truth):
        return {"examples": 0}
    out = {"examples": len(truth)}
    predicted = model.predict(x)
    if target["type"] == "continuous":
        out.update(spearman=score(target, truth, model, x), mae=float(mean_absolute_error(truth, predicted)),
                   baseline_mae=float(mean_absolute_error(truth, baseline.predict(x))))
        return out
    out.update(balanced_accuracy=float(balanced_accuracy_score(truth, predicted)), chance=1 / len(target["classes"]),
               baseline_balanced_accuracy=float(balanced_accuracy_score(truth, baseline.predict(x))),
               macro_f1=float(f1_score(truth, predicted, average="macro", labels=target["classes"], zero_division=0)),
               recall={c: float(np.mean([p == c for p, t in zip(predicted, truth) if t == c])) if c in truth else None for c in target["classes"]})
    seen = list(model.classes_)
    if len(set(truth)) > 1 and set(truth) <= set(seen):
        proba = model.predict_proba(x)
        try:
            out["auc"] = float(roc_auc_score(truth, proba[:, 1], labels=seen) if len(seen) == 2 else roc_auc_score(truth, proba, multi_class="ovr", labels=seen))
        except ValueError:
            pass
    return out


def shuffled_check(recipe, x_train, y_train, x_val, y_val, real, rounds, seed):
    """How often the same model trained on shuffled labels scores at least as well on validation."""
    if rounds <= 0 or not len(y_val):
        return None
    rng = np.random.default_rng(seed)
    scores = []
    for _ in range(rounds):
        y = rng.permutation(y_train)
        if recipe["target"]["type"] != "continuous" and len(set(y)) < 2:
            continue
        m = pipeline_for(recipe["model"]["architecture"], recipe["target"]["type"], recipe["model"]["params"], int(rng.integers(1 << 30)))
        m.fit(x_train, y)
        scores.append(score(recipe["target"], y_val, m, x_val))
    at_least = sum(s >= real for s in scores)
    return {"rounds": len(scores), "at_least_as_good": at_least, "p_value": (1 + at_least) / (1 + len(scores)),
            "shuffled_median": float(np.median(scores)) if scores else None}


def plain_summary(target, report):
    v = report["metrics"].get("validation", {})
    if not v.get("examples"):
        return "No validation examples: label more people or sessions before trusting this model."
    if target["type"] == "continuous":
        head = f"Validation: Spearman {v['spearman']:.2f} on {v['examples']} windows (error {v['mae']:.3f} vs {v['baseline_mae']:.3f} for always guessing the average)."
    else:
        head = f"Validation: balanced accuracy {v['balanced_accuracy']:.2f} on {v['examples']} windows (chance {v['chance']:.2f})."
    s = report.get("shuffled")
    if s and s["rounds"]:
        floor = 1 / (s["rounds"] + 1)
        if s["at_least_as_good"] == 0:
            head += f" It beat all {s['rounds']} models trained on shuffled labels"
            head += f" (p = {s['p_value']:.2f})." if floor <= 0.05 else f"; with so few tries p cannot go below {floor:.2f}, so use --shuffles 19 or more for a firmer check."
        else:
            head += f" Shuffled-label models matched or beat it in {s['at_least_as_good']} of {s['rounds']} tries (p = {s['p_value']:.2f})."
            if s["p_value"] > 0.05:
                head += " Treat it as no better than chance until it has more or better labels."
    return head


def train(workspace, recipe, dataset_dir=None, architecture=None, evaluate_test=False, shuffles=None, log=print):
    if architecture:
        recipe = {**recipe, "model": {**recipe["model"], "architecture": architecture,
                                      "input": "sequence" if architecture in SEQUENCE_ARCHITECTURES else "window_features"}}
    if recipe["model"]["architecture"] in SEQUENCE_ARCHITECTURES:
        from .sequence import train_sequence
        return train_sequence(workspace, recipe, dataset_dir, evaluate_test, log, 3 if shuffles is None else shuffles)
    dataset_dir = dataset_dir or workspace.latest("datasets", recipe["name"])
    manifest, rows = load_dataset(dataset_dir)
    names = manifest["features"]
    if not names:
        raise ValueError("The dataset has no features")
    target = recipe["target"]
    seed = int(hashlib.sha256(recipe["name"].encode()).hexdigest()[:8], 16)
    x = {s: matrix(rows[s], names) for s in SPLITS}
    y = {s: [r["targets"][target["name"]] for r in rows[s]] for s in SPLITS}
    if not rows["train"]:
        raise ValueError("No training examples; label more windows first")
    if target["type"] != "continuous" and len(set(y["train"])) < 2:
        raise ValueError("Training needs at least two different labels")
    model = pipeline_for(recipe["model"]["architecture"], target["type"], recipe["model"]["params"], seed)
    model.fit(x["train"], y["train"])
    baseline = (DummyRegressor() if target["type"] == "continuous" else DummyClassifier(strategy="prior")).fit(x["train"], y["train"])
    report = {"recipe": recipe["name"], "architecture": recipe["model"]["architecture"], "sklearn_version": sklearn.__version__,
              "dataset": str(dataset_dir), "dataset_manifest_sha256": hashlib.sha256((dataset_dir / "manifest.json").read_bytes()).hexdigest(),
              "features": names, "counts": {s: len(rows[s]) for s in SPLITS}, "label_sources": dict(sorted(
                  Counter(f"{r['label']['source']}:{r['label']['version']}" for s in SPLITS for r in rows[s]).items())),
              "metrics": {}, "warnings": list(manifest.get("warnings", [])),
              "curve": boosting_curve(model, target, x["train"], y["train"], x["validation"], y["validation"])}
    for split in ("validation", "test") if evaluate_test else ("validation",):
        report["metrics"][split] = metrics(target, model, baseline, x[split], y[split])
    if rows["validation"]:
        real = score(target, y["validation"], model, x["validation"])
        report["shuffled"] = shuffled_check(recipe, x["train"], y["train"], x["validation"], y["validation"], real, 20 if shuffles is None else shuffles, seed)
    report["summary"] = plain_summary(target, report)
    version = workspace.next_version("models", recipe["name"])
    out = workspace.path("models", recipe["name"], f"v{version}")
    tmp = out.with_name(out.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    spec = export_model(model, names, x["train"], recipe, version, {"summary": report["summary"], "validation": report["metrics"].get("validation")})
    spec["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (tmp / "model.json").write_text(json.dumps(spec, separators=(",", ":"), allow_nan=False))
    (tmp / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    (tmp / "recipe.json").write_text(json.dumps(recipe, indent=2) + "\n")
    joblib.dump({"pipeline": model, "features": names, "target": target}, tmp / "classifier.joblib")
    tmp.rename(out)
    log(f"Trained {recipe['name']} v{version} ({recipe['model']['architecture']}). {report['summary']}")
    return out, report
