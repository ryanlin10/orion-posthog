"""The browser runs exported models with the same probabilities scikit-learn gives (src/model.js via node)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from paradigm_orion.export import export_model
from paradigm_orion.recipe import compile_recipe
from paradigm_orion.train import pipeline_for

ROOT = Path(__file__).resolve().parents[2]
RUNNER = """
import { readFileSync } from 'node:fs';
import { predict } from '%s';
const spec = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const rows = JSON.parse(readFileSync(process.argv[3], 'utf8'));
console.log(JSON.stringify(rows.map(r => predict(spec, r))));
""" % (ROOT / "src" / "model.js").as_uri()


def js_predict(spec, rows):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "model.json").write_text(json.dumps(spec))
        (tmp / "rows.json").write_text(json.dumps(rows))
        (tmp / "run.mjs").write_text(RUNNER)
        out = subprocess.run(["node", tmp / "run.mjs", tmp / "model.json", tmp / "rows.json"], check=True, capture_output=True, text=True)
        return json.loads(out.stdout)


def data(seed, n=240, k=6, classes=("calm", "frustrated")):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, k)) * [1, 10, 100, 0.1, 1, 5]
    x[rng.random(size=x.shape) < 0.15] = np.nan
    x[:, 5] = np.nan  # A feature that is never observed.
    score = np.nan_to_num(x[:, 0]) + 0.1 * np.nan_to_num(x[:, 1]) + rng.normal(scale=0.5, size=n)
    if classes is None:
        return x, np.clip(score / 4 + 0.5, 0, 1)
    bins = np.quantile(score, np.linspace(0, 1, len(classes) + 1)[1:-1])
    return x, np.array([classes[i] for i in np.digitize(score, bins)])


class ExportParityTests(unittest.TestCase):
    names = [f"f{i}_median" for i in range(6)]

    def check(self, architecture, target, classes):
        recipe = compile_recipe({"name": "parity", "target": {"mood": target}, "model": {"architecture": architecture, "trees": 40}})
        x, y = data(7, classes=classes)
        model = pipeline_for(architecture, recipe["target"]["type"], recipe["model"]["params"], 3)
        model.fit(x[:180], y[:180])
        spec = export_model(model, self.names, x[:180], recipe, 1, {})
        rows = [{n: (None if np.isnan(v) else float(v)) for n, v in zip(self.names, row)} for row in x[180:]]
        got = js_predict(spec, rows)
        if classes is None:
            expected = np.clip(model.predict(x[180:]), 0, 1)
            np.testing.assert_allclose([g["value"] for g in got], expected, rtol=1e-6, atol=1e-9)
        else:
            expected = model.predict_proba(x[180:])
            actual = [[g["probabilities"][str(c)] for c in model.classes_] for g in got]
            np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-9)
            self.assertEqual([g["label"] for g in got], list(model.predict(x[180:])))

    def test_classifiers(self):
        for architecture in ("logistic", "random_forest", "boosted_trees"):
            for classes in (("no", "yes"), ("calm", "engaged", "confused", "frustrated")):
                target = "binary" if classes == ("no", "yes") else list(classes)
                with self.subTest(architecture=architecture, classes=len(classes)):
                    self.check(architecture, target, classes)

    def test_scores(self):
        for architecture in ("logistic", "random_forest", "boosted_trees"):
            with self.subTest(architecture=architecture):
                self.check(architecture, "score(0, 1)", None)


if __name__ == "__main__":
    unittest.main()
