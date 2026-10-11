"""Optional learner checks: no holdout fitting and no silent dataset mutation."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from paradigm_orion.dataset.pipeline import dump, fingerprint

HAS_SKLEARN = importlib.util.find_spec("sklearn") is not None


@unittest.skipUnless(HAS_SKLEARN, "Install .[training] for reference-trainer checks")
class TrainerTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("trainer", Path(__file__).resolve().parents[2] / "examples/training/train_classifier.py")
        self.trainer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.trainer)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recipe = self.root / "recipe.json"
        self.recipe.write_text(dump({"target":"mood"}))
        self.manifest = {"recipe":{"targets":{"mood":{"type":"categorical", "classes":["a", "b"]}}},
                         "features":["timing"], "output_sha256":{}}
        for split, values in (("train", [0, 2, None, 4]), ("validation", [10000, None]), ("test", [-10000, None])):
            rows = [{"split":split, "group_id":split, "features":{"timing":v}, "targets":{"mood":"a" if i % 2 else "b"}}
                    for i, v in enumerate(values)]
            path = self.root / f"{split}.jsonl"
            path.write_text("".join(dump(r) + "\n" for r in rows))
            self.manifest["output_sha256"][path.name] = fingerprint(path)
        (self.root / "manifest.json").write_text(dump(self.manifest))

    def test_preprocessing_fits_train_only_and_test_evaluation_is_opt_in(self):
        import joblib
        report = self.trainer.train(self.root, self.recipe, self.root / "model")
        model = joblib.load(self.root / "model/classifier.joblib")["pipeline"]
        self.assertEqual(model.steps[0][1].statistics_.tolist(), [2.0])
        self.assertEqual(model.steps[1][1].mean_[0], 2.0)
        self.assertEqual(set(report["metrics"]), {"validation"})
        self.assertIn("baseline_balanced_accuracy", report["metrics"]["validation"])

    def test_modified_data_fails_checksum(self):
        with (self.root / "train.jsonl").open("a") as f:
            f.write("\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.trainer.train(self.root, self.recipe, self.root / "model")

    def test_declared_group_leakage_fails_even_with_valid_checksums(self):
        path = self.root / "test.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            row["group_id"] = "train"
        path.write_text("".join(dump(row) + "\n" for row in rows))
        self.manifest["output_sha256"][path.name] = fingerprint(path)
        (self.root / "manifest.json").write_text(dump(self.manifest))
        with self.assertRaisesRegex(ValueError, "Group leakage"):
            self.trainer.train(self.root, self.recipe, self.root / "model")
