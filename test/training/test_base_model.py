"""The base model in src/base-model.js: its features are the ones Python computes, and its weights give the
probabilities scikit-learn gave on the real DUX windows in the test fixture (the browser test checks the same in JS)."""
import json
import math
import unittest
from pathlib import Path

from paradigm_orion.dataset.features import extract_features

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "src" / "base-model.js").read_text().split("export const BASE_MODEL = ", 1)[1].rstrip().rstrip(";"))
WINDOWS = json.loads((ROOT / "test" / "fixtures" / "base-model-windows.json").read_text())


def probability(features):
    names, p = SPEC["features"]["names"], SPEC["preprocess"]
    raw = [features[n] if features[n] is not None else math.nan for n in names]
    x = [p["impute"][i] if math.isnan(v) else v for i, v in enumerate(raw)] + [1.0 if math.isnan(raw[i]) else 0.0 for i in p["indicators"]]
    z = sum(w * (v - m) / s for w, v, m, s in zip(SPEC["estimator"]["coef"][0], x, p["mean"], p["scale"])) + SPEC["estimator"]["intercept"][0]
    second = 1 / (1 + math.exp(-z))  # A binary logistic model scores the second of its (alphabetical) classes.
    return second if SPEC["estimator"]["classes"][1] == "negative" else 1 - second


class BaseModelTests(unittest.TestCase):
    def test_features_are_typing_and_motion_without_counts(self):
        computed = extract_features([], {"start_ms": 0, "end_ms": 10000}, SPEC["features"]["families"])[0]
        self.assertEqual(SPEC["features"]["names"], sorted(set(computed) - {"motion_moves", "typing_pause_count"}))
        self.assertEqual((SPEC["target"]["classes"], SPEC["decision"]), (["neutral", "negative"], {"class": "negative", "min_probability": 0.25}))
        self.assertIn("CC BY 4.0", SPEC["about"]["trained_on"])

    def test_weights_reproduce_the_training_probabilities(self):
        for w in WINDOWS:
            features = extract_features(w["events"], {"start_ms": 0, "end_ms": 10000}, SPEC["features"]["families"])[0]
            self.assertAlmostEqual(probability(features), w["probability"], places=9)
        self.assertEqual({w["label"] for w in WINDOWS}, {"negative", "neutral"})


if __name__ == "__main__":
    unittest.main()
