"""The whole loop on generated data: PostHog export → labels → dataset → classifier → live labels in the
browser runtime → context and search for agents. The generated people have a planted calm/frustrated signal."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from fakes import FakePostHog, ScriptedModel
from paradigm_orion import Orion
from paradigm_orion.synthetic import generate
from paradigm_orion.workspace import read_jsonl

HERE = Path(__file__).resolve().parent
RECIPE = """
name: checkout-sentiment
source:
  since: 30d
  pages: [/checkout, /cart]
target:
  sentiment: [calm, frustrated]
window:
  length: 10s
  align: past
telemetry: [pointer, keyboard, scroll, focus]
labels:
  by: [human, model]
  min_confidence: 0.6
  model:
    reads: [text, replay]
    llm: scripted-rater
split:
  by: [user]
  train: 0.6
  validation: 0.2
  test: 0.2
model:
  architecture: boosted_trees
  trees: 150
deploy:
  live: true
  context: [support-agent]
  every: 2s
"""


def state_at(truth, session_id, t):
    for seg in truth:
        if seg["session_id"] == session_id and seg["start_ms"] <= t < seg["end_ms"]:
            return seg["state"]
    return None


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.truth = generate(people=30, sessions_per_person=3, seed=4)
        cls.fake = FakePostHog(cls.rows)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "recipes").mkdir()
        cls.recipe_path = cls.root / "recipes" / "checkout-sentiment.yaml"
        cls.recipe_path.write_text(RECIPE)
        cls.llm = ScriptedModel()
        cls.orion = Orion(posthog_project="1", host=cls.fake.url, api_key="phx_test", home=cls.root / ".orion", llm=cls.llm, log=lambda *a: None)
        cls.orion.posthog.sleep = lambda s: None
        cls.orion.posthog.poll_seconds = 0
        cls.pulled = cls.orion.pull("30d", recipe=cls.recipe_path)
        cls.clf = cls.orion.train(cls.recipe_path)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        cls.tmp.cleanup()

    def test_pull_uses_file_exports_and_never_leaks_the_key(self):
        self.assertGreater(self.pulled["telemetry_events"], 100)
        self.assertEqual(self.pulled["recordings"], 90)
        self.assertFalse([r for r in self.fake.requests if r[0] == "POST" and r[1].endswith("/query/")])
        self.assertEqual(self.fake.signed_with_auth, 0)
        self.assertTrue(any(r[1].startswith("/signed/") for r in self.fake.requests))

    def test_labellers_agree_with_the_planted_state(self):
        labels = {name: read_jsonl(self.root / ".orion" / "labels" / "checkout-sentiment" / f"{name}.jsonl") for name in ("text", "replay")}
        recs = {r["recorder_id"]: r for r in self.orion.store.recordings()}
        for name, rows in labels.items():
            self.assertGreater(len(rows), 50, name)
            right = 0
            for label in rows:
                mid = (label["start_ms"] + label["end_ms"]) / 2
                truth = next((t["state"] for t in self.truth if t["recorder_id"] == label["recorder_id"] and t["start_ms"] <= mid < t["end_ms"]), None)
                right += truth == label["targets"]["sentiment"]
            self.assertGreater(right / len(rows), 0.75, name)
            self.assertTrue(all(recs[l["recorder_id"]] for l in rows))

    def test_classifier_beats_chance_and_shuffled_labels(self):
        v = self.clf.report["metrics"]["validation"]
        self.assertGreater(v["examples"], 40)
        self.assertGreater(v["balanced_accuracy"], 0.75)
        self.assertLessEqual(self.clf.report["shuffled"]["p_value"], 0.1)
        self.assertIn("Validation: balanced accuracy", self.clf.summary)

    def test_live_labels_in_the_browser_follow_the_planted_state(self):
        deployed = self.clf.deploy(out=self.root / "public" / "orion")
        spec = json.loads(Path(deployed["file"]).read_text())
        self.assertEqual(spec["agents"], ["support-agent"])
        dataset = json.loads((self.root / ".orion" / "datasets" / "checkout-sentiment" / "v1" / "manifest.json").read_text())
        held_out = {g for g, s in dataset["groups"].items() if s != "train"}
        sessions = {}
        for row in self.rows:
            if row["event"] == "orion_input" and row["person_id"] in held_out:
                p = row["properties"]
                sessions.setdefault(p["$session_id"], []).extend(p["records"])
        payload = [{"session_id": s, "records": sorted(r, key=lambda x: x["t"])} for s, r in sessions.items()]
        (self.root / "sessions.json").write_text(json.dumps(payload))
        out = subprocess.run(["node", HERE / "live_sim.mjs", deployed["file"], self.root / "sessions.json"], check=True, capture_output=True, text=True)
        result = json.loads(out.stdout)
        right = total = 0
        for s in result["sessions"]:
            for tick in s["ticks"]:
                truth = state_at(self.truth, s["session_id"], tick["t"] - 5000)
                if tick["label"] and truth:
                    total += 1
                    right += tick["label"] == truth
        self.assertGreater(total, 200)
        self.assertGreater(right / total, 0.7)
        sent = [e for e in result["events"] if e["name"] == "orion_label"]
        self.assertTrue(sent and all(e["props"]["model"] == "checkout-sentiment" and e["props"]["agents"] == ["support-agent"] for e in sent))
        type(self).live_events = sent

    def test_zz_context_and_search_read_the_labels_back(self):
        events = getattr(type(self), "live_events", None)
        if not events:
            self.skipTest("live test did not run")
        for i, e in enumerate(events):
            self.fake.labels.append({"timestamp": f"2026-10-11T12:{i // 60 % 60:02d}:{i % 60:02d}Z", "person_id": "person-" + e["session_id"].split("-")[1],
                                     "properties": {**e["props"], "$session_id": e["session_id"], "$pathname": "/checkout"}})
        session = events[-1]["session_id"]
        ctx = self.orion.context(session)
        self.assertEqual(ctx["labels"][0]["model"], "checkout-sentiment")
        self.assertIn("/replay/" + session, ctx["replay_url"])
        text = self.orion.context(session, text=True)
        self.assertIn("user_state:", text)
        found = self.orion.search(label="frustrated", page="/checkout")
        self.assertTrue(found["sessions"])
        self.assertEqual(found["pages"][0]["page"], "/checkout")
        # A cohort narrows the search to its people, found by name or by id.
        person = self.fake.labels[0]["person_id"]
        self.fake.cohorts = {7: {"name": "Paying customers", "people": {person}}, 8: {"name": "Trial", "people": set()}}
        for cohort in ("Paying customers", "7", 7):
            narrowed = self.orion.search(page="/checkout", cohort=cohort)
            self.assertEqual(narrowed["cohort"], {"id": 7, "name": "Paying customers"})
            self.assertTrue(narrowed["sessions"])
            self.assertTrue(all(s["session_id"].split("-")[1] == person.split("-")[1] for s in narrowed["sessions"]))
        self.assertLess(len(narrowed["sessions"]), len(self.orion.search(page="/checkout")["sessions"]))
        with self.assertRaisesRegex(ValueError, "Found 0 cohorts"):
            self.orion.search(cohort="Nobody")
        query = [r for r in self.fake.requests if r[0] == "POST" and r[1].endswith("/query/")]
        self.assertTrue(any("person_id IN COHORT 7" in r[2]["query"]["query"] for r in query))
        self.assertTrue(all(r[2]["query"]["kind"] == "HogQLQuery" and "values" in r[2]["query"] for r in query))


if __name__ == "__main__":
    unittest.main()
