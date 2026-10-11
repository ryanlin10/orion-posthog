"""Trained scikit-learn pipelines → the portable "orion-model" JSON that src/model.js runs in the browser.

Pipelines are SimpleImputer(median, add_indicator) → StandardScaler → estimator. Supported estimators:
logistic/linear (LogisticRegression, Ridge), forests (RandomForest*), and boosted trees (GradientBoosting*).
"""
import math

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor, RandomForestClassifier, RandomForestRegressor
from sklearn.linear_model import LogisticRegression, Ridge

FORMAT = {"format": "orion-model", "format_version": 1}


def _floats(values):
    return [float(v) for v in np.asarray(values, dtype=float).ravel()]


def _tree(tree, classifier):
    t = tree.tree_
    if classifier:
        value = t.value[:, 0, :]
        totals = value.sum(axis=1, keepdims=True)
        value = np.divide(value, totals, out=np.zeros_like(value), where=totals > 0)
    else:
        value = t.value[:, 0, :1]
    return {"feature": [int(f) for f in t.feature], "threshold": _floats(t.threshold),
            "left": [int(c) for c in t.children_left], "right": [int(c) for c in t.children_right],
            "value": [[round(float(v), 9) for v in row] for row in value]}


def estimator_spec(estimator, sample):
    """sample: one preprocessed row, used to recover the boosted model's starting score through public APIs."""
    if isinstance(estimator, LogisticRegression):
        return {"kind": "linear", "coef": [_floats(row) for row in estimator.coef_], "intercept": _floats(estimator.intercept_),
                "classes": [str(c) for c in estimator.classes_]}
    if isinstance(estimator, Ridge):
        return {"kind": "linear", "coef": [_floats(np.atleast_2d(estimator.coef_)[0])], "intercept": [float(np.ravel(estimator.intercept_)[0])]}
    if isinstance(estimator, RandomForestClassifier):
        return {"kind": "forest", "trees": [_tree(t, True) for t in estimator.estimators_], "classes": [str(c) for c in estimator.classes_]}
    if isinstance(estimator, RandomForestRegressor):
        return {"kind": "forest", "trees": [_tree(t, False) for t in estimator.estimators_]}
    if isinstance(estimator, (GradientBoostingClassifier, GradientBoostingRegressor)):
        stages = estimator.estimators_
        rate = float(estimator.learning_rate)
        trees = [[_tree(t, False) for t in stage] for stage in stages]
        x = np.asarray(sample, dtype=float).reshape(1, -1)
        added = np.array([[rate * t.predict(x)[0] for t in stage] for stage in stages]).sum(axis=0)
        if isinstance(estimator, GradientBoostingClassifier):
            raw = np.atleast_1d(estimator.decision_function(x)[0])
            return {"kind": "boosted", "init": _floats(raw - added), "learning_rate": rate, "trees": trees, "classes": [str(c) for c in estimator.classes_]}
        raw = np.atleast_1d(estimator.predict(x)[0])
        return {"kind": "boosted", "init": _floats(raw - added), "learning_rate": rate, "trees": trees}
    raise TypeError(f"Cannot export {type(estimator).__name__} for the browser")


def importance(estimator, n_features):
    if hasattr(estimator, "coef_"):
        values = np.abs(np.atleast_2d(estimator.coef_)).mean(axis=0)
    elif hasattr(estimator, "feature_importances_"):
        values = estimator.feature_importances_
    else:
        return None
    values = np.asarray(values[:n_features], dtype=float)
    top = values.max() if values.size and values.max() > 0 else 1.0
    return values / top


def export_model(pipeline, names, train_matrix, recipe, version, report_summary):
    imputer, scaler, estimator = (step for _, step in pipeline.steps)
    statistics = np.where(np.isnan(imputer.statistics_), 0.0, imputer.statistics_)
    indicators = [int(i) for i in imputer.indicator_.features_] if imputer.indicator_ is not None else []
    sample = scaler.transform(imputer.transform(train_matrix[:1]))[0]
    target = recipe["target"]
    spec = {**FORMAT, "name": recipe["name"], "version": version,
            "target": {k: target[k] for k in ("name", "type", "classes", "min", "max", "binary") if k in target},
            "window": {"size_ms": recipe["window"]["length_ms"], "align": recipe["window"]["align"],
                       "min_events": recipe["quality"]["min_events"], "max_events": 100000, "allow_gaps": not recipe["quality"]["drop_gaps"]},
            "features": {"families": list(recipe["telemetry"]), "exclude": recipe["features"]["exclude"], "names": list(names)},
            "preprocess": {"impute": _floats(statistics), "indicators": indicators, "mean": _floats(scaler.mean_), "scale": _floats(scaler.scale_)},
            "estimator": estimator_spec(estimator, sample),
            "agents": recipe["deploy"]["context"],
            "live": {"every_ms": recipe["deploy"]["every_ms"], "heartbeat_ms": recipe["deploy"]["heartbeat_ms"], "confirm": recipe["deploy"]["confirm"]},
            "report": report_summary}
    # Plain-words evidence compares live values with the training medians, scaled by their spread.
    reference, spread = {}, {}
    for j, name in enumerate(names):
        column = train_matrix[:, j]
        column = column[~np.isnan(column)]
        if column.size:
            q1, median, q3 = np.percentile(column, [25, 50, 75])
            reference[name] = float(median)
            width = float(q3 - q1) or float(column.std())
            if width > 0 and math.isfinite(width):
                spread[name] = width
    weights = importance(estimator, len(names))
    spec["evidence"] = {"reference": reference, "spread": spread,
                        "importance": {n: round(float(w), 4) for n, w in zip(names, weights)} if weights is not None else {}}
    return spec
