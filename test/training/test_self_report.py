"""Self-report: recipes, labels from people's own answers, Jev-ranked survey files, and deploy. Generated data only;
Jev and the AI labeller are local stand-ins."""
import json
import os
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from paradigm_orion import Orion
from paradigm_orion.labellers.self_report import answer_value
from paradigm_orion.recipe import RecipeError, compile_recipe
from paradigm_orion.store import slim_context
from paradigm_orion.synthetic import generate, write_export

RECIPE = """
name: checkout-sentiment
target:
  sentiment: [calm, engaged, confused, frustrated]
window:
  length: 10s
telemetry: [pointer, keyboard, scroll]
labels:
  by: [self_report]
split:
  by: [user, session]
model:
  architecture: boosted_trees
  trees: 60
"""


def base(**changes):
    raw = {"name": "x", "target": {"sentiment": ["calm", "engaged", "confused", "frustrated"]}}
    raw.update(changes)
    return compile_recipe(raw)


class RecipeTests(unittest.TestCase):
    def test_answers_come_from_the_target_classes_or_the_standard_lists(self):
        offered = base()["labels"]["self_report"]
        self.assertEqual((offered["positive"], offered["negative"]), (["calm", "engaged"], ["confused", "frustrated"]))
        self.assertEqual(offered["question"], "How is it going so far?")
        binary = compile_recipe({"name": "x", "target": {"frustrated": "binary"}})["labels"]["self_report"]
        self.assertEqual(binary["positive"], ["calm", "pleased", "engaged", "confident"])
        custom = base(labels={"by": ["self_report"], "self_report": {"question": "How was checkout?", "positive": ["calm", "happy"], "negative": ["stuck", "annoyed"]}})
        self.assertEqual(custom["labels"]["self_report"]["negative"], ["stuck", "annoyed"])
        with self.assertRaises(RecipeError):
            base(labels={"self_report": {"positive": ["calm"]}})
        with self.assertRaises(RecipeError):
            base(labels={"self_report": {"positive": ["calm", "x"], "negative": ["calm", "y"]}})

    def test_page_names_and_combined_splits_are_accepted(self):
        self.assertEqual(base(model={"architecture": "temporal_cnn"})["model"]["architecture"], "tcn")
        self.assertEqual(base(model={"architecture": "temporal_cnn"})["model"]["input"], "sequence")
        self.assertEqual(base(split={"by": ["user", "session"]})["split"]["by"], ["user"])
        self.assertEqual(base(split={"by": ["user", "day", "session"]})["split"]["by"], ["user", "day"])
        self.assertEqual(base(split={"by": ["session"]})["split"]["by"], ["session"])
        self.assertIsNone(base()["labels"]["model"]["llm"], "no llm: the model chosen at orion init")

    def test_answers_map_onto_each_kind_of_target(self):
        classes = base()
        self.assertEqual(answer_value(classes, "confused"), "confused")
        self.assertIsNone(answer_value(classes, "angry"), "not one of this target's classes")
        binary = compile_recipe({"name": "x", "target": {"frustrated": "binary"}})
        self.assertEqual([answer_value(binary, a) for a in ("frustrated", "calm", "confused")], ["yes", "no", None])
        score = compile_recipe({"name": "x", "target": {"frustration": "score(0, 1)"}})
        self.assertEqual([answer_value(score, a) for a in ("frustrated", "pleased", "anxious")], [1.0, 0.0, None])

    def test_pulled_answers_keep_what_the_labeller_needs(self):
        row = {"uuid": "u", "event": "orion_self_report", "timestamp": "2026-10-12T10:00:00Z", "properties": {
            "$session_id": "s", "$pathname": "/checkout", "answer": "calm", "options": ["calm", "engaged", "confused", "frustrated"],
            "recorder_id": "r", "shown_t": 61000.5, "answered_t": 63000.0, "dismissed": False, "mood": "calm", "$browser": "Chrome"}}
        detail = slim_context(row, ())["detail"]
        self.assertEqual((detail["answer"], detail["recorder_id"], detail["shown_t"]), ("calm", "r", 61000.5))
        self.assertNotIn("$browser", detail)


class FakeJev:
    """Ranks answers like a careful rater: shaking means frustration, circling confusion, steady movement calm."""

    def __init__(self):
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.headers.get("Authorization"), body))
                state = body["state"]
                lead = "frustrated" if "shaking" in state else "confused" if "circling" in state else "calm"
                answers = {}
                for name, q in body["questions"].items():
                    options = list(q["criteria"])
                    p = {o: (0.7 if o == lead else 0.3 / max(1, len(options) - 1)) for o in options}
                    answers[name] = {"type": "choice", "choice": max(p, key=p.get), "probabilities": p}
                data = json.dumps({"answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SelfReportPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        rows, truth = generate(people=24, sessions_per_person=2, seed=3)
        # People answer the question at the end of some stretches of their session: how it was going is the planted state.
        first = {}
        for r in rows:
            if r["event"] == "orion_input":
                first.setdefault(r["properties"]["recorder_id"], r)
        cls.answers = 0
        for i, seg in enumerate(truth):
            if seg["end_ms"] - seg["start_ms"] < 10000 or i % 2:
                continue
            anchor = first[seg["recorder_id"]]
            rows.append({"uuid": str(uuid.UUID(int=i + 1)), "event": "orion_self_report", "timestamp": anchor["timestamp"],
                         "distinct_id": anchor["distinct_id"], "person_id": anchor["person_id"],
                         "properties": {"$session_id": seg["session_id"], "$pathname": "/checkout", "answer": seg["state"],
                                        "options": ["calm", "engaged", "confused", "frustrated"], "recorder_id": seg["recorder_id"],
                                        "shown_t": seg["end_ms"], "answered_t": seg["end_ms"] + 2500, "dismissed": False}})
            cls.answers += 1
        rows.append({"uuid": str(uuid.UUID(int=99999)), "event": "orion_self_report", "timestamp": rows[0]["timestamp"], "distinct_id": "d",
                     "properties": {"$session_id": "s", "answer": None, "dismissed": True, "recorder_id": "r", "shown_t": 1.0}})
        write_export(rows, cls.root / "export.jsonl.gz")
        (cls.root / "recipe.yaml").write_text(RECIPE)
        cls.orion = Orion(posthog_project="1", api_key="phx_test", home=cls.root / ".orion", log=lambda *a: None)
        cls.orion.store.import_files([cls.root / "export.jsonl.gz"], "synthetic")
        cls.orion.store.index()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_answers_label_the_input_before_the_question_and_train_a_classifier(self):
        out, summary = self.orion.build(self.root / "recipe.yaml")
        stats = summary["label_runs"]["self_report"]
        self.assertEqual(stats["labelled"], self.answers)
        self.assertEqual(stats["dismissed"], 1)
        labels = [json.loads(line) for line in (self.root / ".orion" / "labels" / "checkout-sentiment" / "self_report.jsonl").read_text().splitlines()]
        self.assertTrue(all(l["source"] == "self_report" and l["end_ms"] - l["start_ms"] == 10000 for l in labels))
        clf = self.orion.train(self.root / "recipe.yaml", rebuild=False, shuffles=2)
        self.assertEqual(clf.report["label_sources"], {"self_report:self-report-1": sum(clf.report["counts"].values())})
        self.assertGreater(clf.report["metrics"]["validation"]["balanced_accuracy"], 0.3)
        curve = clf.report["curve"]
        self.assertEqual(curve["unit"], "trees")
        self.assertGreater(curve["epochs"][0]["train"], curve["epochs"][-1]["train"], "boosting lowers the training loss")

    def test_jev_ranks_answers_per_page_and_mood_and_deploy_ships_them(self):
        jev = FakeJev()
        self.addCleanup(jev.close)
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts-test", "ORION_TYPESAFE_URL": jev.url}):
            survey, path = self.orion.survey(self.root / "recipe.yaml", pages=5)
        self.assertEqual(survey["format"], "orion-survey")
        self.assertEqual(survey["ranked_by"], "jev-1.13.0")
        self.assertEqual(set(survey["table"]["*"]), {"angry", "frustrated", "calm", "anxious", "confused", "pleased", "quiet"})
        self.assertEqual(survey["table"]["*"]["frustrated"][2], "frustrated", "shaking ranks frustrated first among the negatives")
        self.assertEqual(survey["table"]["*"]["confused"][2], "confused")
        self.assertTrue(all(auth == "Bearer ts-test" for auth, _ in jev.requests))
        self.assertTrue(all(len(order) == 4 and set(order[:2]) <= {"calm", "engaged"} for page in survey["table"].values() for order in page.values()))
        self.assertTrue(path.exists())
        calls = len(jev.requests)
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts-test", "ORION_TYPESAFE_URL": jev.url}):
            self.orion.survey(self.root / "recipe.yaml", pages=5)
        self.assertEqual(len(jev.requests), calls, "answers are cached")

        # Deploy copies the survey next to the model and names it in the snippet.
        clf = self.orion.train(self.root / "recipe.yaml", rebuild=True, shuffles=0, pull=False)  # imported data: nothing to pull
        deployed = clf.deploy(out=self.root / "public")
        self.assertTrue((self.root / "public" / "checkout-sentiment-survey.json").exists())
        self.assertIn("selfReport: { survey: '/orion/checkout-sentiment-survey.json' }", deployed["snippet"])
        self.assertIn("https://cdn.paradigmmachines.ai/orion@0.4.0/orion.js", deployed["snippet"])

    def test_without_a_typesafe_key_the_ai_labeller_ranks(self):
        class Ranker:
            model = "stand-in-llm"

            def complete(self, system, prompt):
                return json.dumps({"frustrated": {"positive": ["engaged", "calm"], "negative": ["frustrated", "confused"]}})

        orion = Orion(posthog_project="1", api_key="phx_test", home=self.root / ".orion", llm=Ranker(), log=lambda *a: None)
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "", "ORION_KEYS_FILE": str(self.root / "no-keys.env")}):
            survey, _ = orion.survey(self.root / "recipe.yaml", pages=2)
        self.assertEqual(survey["ranked_by"], "stand-in-llm")
        self.assertEqual(survey["table"]["*"]["frustrated"], ["engaged", "calm", "frustrated", "confused"])
        self.assertEqual(survey["table"]["*"]["calm"], ["calm", "engaged", "confused", "frustrated"], "missing moods keep the listed order")


if __name__ == "__main__":
    unittest.main()
