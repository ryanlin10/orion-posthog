"""Optional reference trainer. The dataset builder itself needs only Python's standard library."""
import argparse
import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import balanced_accuracy_score, classification_report, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def train(dataset, recipe_path, output, evaluate_test=False):
    dataset, output = Path(dataset), Path(output)
    config = json.loads(Path(recipe_path).read_text())
    if set(config) - {"target", "estimator", "parameters", "seed"}:
        raise ValueError("Unknown trainer option")
    manifest = json.loads((dataset / "manifest.json").read_text())
    target = config["target"]
    definition = manifest["recipe"]["targets"][target]
    if definition["type"] != "categorical":
        raise ValueError("Reference trainer expects a categorical target; use your trainer for other target types")
    names = manifest["features"]
    if not names:
        raise ValueError("Dataset has no features")
    rows, matrices = {}, {}
    for split in ("train", "validation", "test"):
        path = dataset / f"{split}.jsonl"
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest["output_sha256"][path.name]:
            raise ValueError("Dataset checksum does not match its manifest")
        rows[split] = [json.loads(line) for line in content.splitlines() if line]
        if any(r["split"] != split for r in rows[split]):
            raise ValueError("Split metadata mismatch")
        matrices[split] = np.array([[r["features"][n] if r["features"][n] is not None else np.nan for n in names] for r in rows[split]], dtype=float)
    group_sets = [{r["group_id"] for r in rows[s]} for s in ("train", "validation", "test")]
    if any(group_sets[i] & group_sets[j] for i in range(3) for j in range(i + 1, 3)):
        raise ValueError("Group leakage across splits")
    y = [r["targets"][target] for r in rows["train"]]
    if len(set(y)) < 2:
        raise ValueError("Training requires at least two observed classes")
    params = {"random_state": config.get("seed", 0), **config.get("parameters", {})}
    if config.get("estimator", "logistic") == "logistic":
        estimator = LogisticRegression(**{"max_iter": 1000, **params})
    elif config["estimator"] == "random_forest":
        estimator = RandomForestClassifier(**{"n_estimators": 200, "n_jobs": 1, **params})
    else:
        raise ValueError("Unknown estimator; choose logistic or random_forest")
    model = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True, add_indicator=True), StandardScaler(), estimator)
    # All learned preprocessing is fitted on train only. No feature, quality or label metadata is appended to X.
    model.fit(matrices["train"], y)
    baseline = DummyClassifier(strategy="prior").fit(matrices["train"], y)
    report = {"sklearn_version": sklearn.__version__, "recipe": config, "features": names,
              "dataset_manifest_sha256": hashlib.sha256((dataset / "manifest.json").read_bytes()).hexdigest(),
              "classes_seen_in_train": list(model.classes_), "metrics": {}}
    for split in (["validation", "test"] if evaluate_test else ["validation"]):
        if not rows[split]:
            report["metrics"][split] = {"examples": 0}
            continue
        truth = [r["targets"][target] for r in rows[split]]
        predicted = model.predict(matrices[split])
        report["metrics"][split] = {"examples": len(truth), "balanced_accuracy": balanced_accuracy_score(truth, predicted),
            "baseline_balanced_accuracy": balanced_accuracy_score(truth, baseline.predict(matrices[split])),
            "classification": classification_report(truth, predicted, labels=definition["classes"], output_dict=True, zero_division=0)}
        if set(truth) <= set(model.classes_):
            report["metrics"][split]["log_loss"] = log_loss(truth, model.predict_proba(matrices[split]), labels=model.classes_)
    output.mkdir(parents=True, exist_ok=False)
    joblib.dump({"pipeline": model, "features": names, "target": target}, output / "classifier.joblib")
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", required=True); p.add_argument("--recipe", required=True); p.add_argument("--out", required=True)
    p.add_argument("--evaluate-test", action="store_true", help="Only after choosing the recipe using validation")
    a = p.parse_args()
    print(json.dumps(train(a.dataset, a.recipe, a.out, a.evaluate_test), indent=2))
