"""`orion train` is the whole loop in one command: it pulls what is new, labels, builds, trains and checks, deploys only
a model that passed its checks, and reports as JSON for agents. Generated data, a local PostHog stand-in."""
import contextlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fakes import FakePostHog
from paradigm_orion.cli import main
from paradigm_orion.synthetic import generate

ROOT = Path(__file__).resolve().parents[2]


class OneCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, truth = generate(people=30, sessions_per_person=2, seed=21)
        first = {}
        for r in rows:
            if r["event"] == "orion_input":
                first.setdefault(r["properties"]["recorder_id"], r)
        # People answer the question at the end of half the stretches: the answer is the planted state.
        for i, seg in enumerate(truth):
            if seg["end_ms"] - seg["start_ms"] >= 10000 and not i % 2:
                a = first[seg["recorder_id"]]
                rows.append({"uuid": str(uuid.UUID(int=i + 1)), "event": "orion_self_report", "timestamp": a["timestamp"],
                             "distinct_id": a["distinct_id"], "person_id": a["person_id"],
                             "properties": {"$session_id": seg["session_id"], "answer": seg["state"], "recorder_id": seg["recorder_id"],
                                            "shown_t": seg["end_ms"], "answered_t": seg["end_ms"] + 2000, "dismissed": False}})
        cls.fake = FakePostHog(rows)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "recipes").mkdir()
        # The shipped recipe, without the AI labeller (no AI key here).
        shipped = (ROOT / "recipes" / "site-feelings.yaml").read_text()
        (cls.root / "recipes" / "site-feelings.yaml").write_text(shipped.replace("by: [self_report, human, model]", "by: [self_report, human]"))
        cls.env = mock.patch.dict(os.environ, {"POSTHOG_HOST": cls.fake.url, "POSTHOG_PROJECT_ID": "1", "POSTHOG_PERSONAL_API_KEY": "phx_test",
                                               "ORION_HOME": str(cls.root / ".orion")})
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.fake.close()
        cls.tmp.cleanup()

    def run_orion(self, *args):
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(args))
        finally:
            os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()

    def test_a_model_that_cannot_show_it_beat_chance_is_not_deployed(self):
        code, out, err = self.run_orion("train", "--deploy", "--json", "--shuffles", "4")
        result = json.loads(out)  # stdout is only the JSON; progress went to stderr
        self.assertEqual(code, 1)
        self.assertIsNone(result["deployed"])
        self.assertIn("--shuffles 19", result["not_ready_because"])
        self.assertIn("exporting from PostHog", err)
        self.assertFalse((self.root / "public" / "orion" / "site-feelings.json").exists())

    def test_one_command_pulls_trains_checks_and_deploys(self):
        exports = sum(1 for r in self.fake.requests if r[0] == "POST" and r[1].endswith("file_download_batch_exports/"))
        code, out, _ = self.run_orion("train", "--deploy", "--json", "--shuffles", "19")
        result = json.loads(out)
        self.assertEqual(code, 0, result["not_ready_because"])
        self.assertGreater(sum(1 for r in self.fake.requests if r[0] == "POST" and r[1].endswith("file_download_batch_exports/")), exports,
                           "it pulled from PostHog itself")
        self.assertEqual(result["recipe"], "site-feelings")
        self.assertGreater(result["validation"]["balanced_accuracy"], 0.7)
        self.assertEqual(result["label_sources"], {"self_report:self-report-1": sum(json.loads(
            (Path(result["model"]) / "report.json").read_text())["counts"].values())})
        spec = json.loads((self.root / "public" / "orion" / "site-feelings.json").read_text())
        self.assertEqual(spec["estimator"]["kind"], "linear", "the shipped recipe starts with logistic regression")
        self.assertIn("/orion/site-feelings.json", result["deployed"]["snippet"])

    def test_the_recipe_must_be_named_when_there_are_several(self):
        extra = self.root / "recipes" / "other.yaml"
        extra.write_text("name: other\ntarget: {feeling: [calm, frustrated]}\n")
        try:
            code, _, err = self.run_orion("train", "--no-pull")
        finally:
            extra.unlink()
        self.assertEqual(code, 1)
        self.assertIn("recipes/ has 2 recipes (other, site-feelings); name the one to use", err)


if __name__ == "__main__":
    unittest.main()
