"""The schema 1.4 signals end to end: recipes with the new families, per-person baselines and outcome labels from goal
events, built and trained through Orion on generated data with a local PostHog stand-in."""
import json
import random
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fakes import FakePostHog
from paradigm_orion import Orion
from paradigm_orion.dataset.pipeline import decode_records, key_type
from paradigm_orion.recipe import RecipeError, builder_recipe, compile_recipe
from paradigm_orion.store import Clock
from paradigm_orion.synthetic import generate
from paradigm_orion.workspace import read_jsonl

RECIPE = """
name: checkout-outcome
target:
  outcome: [purchased, left]
window:
  length: 10s
telemetry: [pointer, keyboard, scroll, fills, visibility, depth, overshoot]
features:
  baseline: true
labels:
  by: [outcome]
  outcome:
    events:
      purchased: [order completed, /checkout/done]
    otherwise: left
    within: 2m
    skip: 5s
    windows: 4
split:
  by: [user]
  train: 0.6
  validation: 0.2
  test: 0.2
model:
  architecture: boosted_trees
  trees: 40
"""


def base(**changes):
    raw = {"name": "x", "target": {"outcome": ["purchased", "left"]}}
    raw.update(changes)
    return compile_recipe(raw)


class RecipeTests(unittest.TestCase):
    def test_new_families_and_time_away(self):
        recipe = base(telemetry=["pointer", "fills", "visibility", "depth", "overshoot", "corrections"])
        self.assertTrue(recipe["quality"]["keep_away"])
        config = builder_recipe(recipe)
        self.assertTrue(config["quality"]["allow_away"])
        self.assertIsNone(config["features"]["baseline"])
        self.assertFalse(builder_recipe(base())["quality"]["allow_away"])
        with self.assertRaises(RecipeError):
            base(telemetry=["pointer", "typing_speed"])

    def test_baselines_need_a_tabular_model(self):
        recipe = base(features={"baseline": True}, window={"length": "8s"})
        self.assertEqual(recipe["features"]["baseline"], {"tile_ms": 8000, "grace_ms": 1000, "cap": 100, "min_tiles": 3})
        with self.assertRaises(RecipeError):
            base(features={"baseline": True}, model={"architecture": "gru"})
        with self.assertRaises(RecipeError):
            base(features={"baseline": "yes"})

    def test_outcome_settings(self):
        recipe = base(labels={"by": ["outcome"], "outcome": {"events": {"purchased": "order completed"}, "otherwise": "left", "within": "10m"}})
        self.assertEqual(recipe["labels"]["outcome"], {"events": {"purchased": ["order completed"]}, "otherwise": "left",
                                                       "within_ms": 600000, "skip_ms": 5000, "windows": 6})
        # YAML reads yes/no keys as booleans; a binary target takes them as its classes.
        binary = compile_recipe({"name": "x", "target": {"converted": "binary"},
                                 "labels": {"by": ["outcome"], "outcome": {"events": {True: ["signup"]}, "otherwise": False}}})
        self.assertEqual((binary["labels"]["outcome"]["events"], binary["labels"]["outcome"]["otherwise"]), ({"yes": ["signup"]}, "no"))
        for bad in ({"events": {"bought": ["x"]}}, {"events": {"purchased": []}}, {"events": {"purchased": ["x"]}, "otherwise": "purchased"},
                    {"events": {"purchased": ["x"]}, "when": "later"}):
            with self.assertRaises(RecipeError):
                base(labels={"by": ["outcome"], "outcome": bad})
        with self.assertRaises(RecipeError):
            base(labels={"by": ["outcome"]})
        with self.assertRaises(RecipeError):
            compile_recipe({"name": "x", "target": {"score": "score(0, 1)"}, "labels": {"by": ["outcome"], "outcome": {"events": {"1": ["x"]}}}})


class BuilderTests(unittest.TestCase):
    def test_key_classes_come_from_the_types_mask_or_the_code(self):
        self.assertEqual([key_type(c) for c in ("Backspace", "KeyZ", "Numpad3", "ArrowDown", "End", "MetaLeft", "Slash")],
                         ["delete", "letter", "digit", "navigation", "navigation", "modifier", "other"])
        ok = {"records": [{"kind": "input", "t": 1, "via": "paste"}, {"kind": "visible", "t": 2, "hidden_ms": 900}, {"kind": "keydown", "t": 3, "key_type": "delete"}]}
        self.assertEqual(len(decode_records(ok)), 3)
        for bad in ({"kind": "input", "t": 1, "via": "typed"}, {"kind": "visible", "t": 1}, {"kind": "keydown", "t": 1, "key_type": "KeyA"},
                    {"kind": "scroll", "t": 1, "content_height": float("nan")}):
            with self.assertRaises(ValueError):
                decode_records({"records": [bad]})


def with_signals(rows, seed=7):
    """Adds what schema 1.4 records to generated sessions, a device id per person, and goal events: sessions that end
    calm go on to buy (half by the order event, half by the confirmation page)."""
    rng = random.Random(seed)
    for row in rows:
        p = row["properties"]
        row["properties"]["$device_id"] = f"device-{row['person_id']}"
        if row["event"] != "orion_input":
            continue
        p["schema_version"] = "1.4"
        extra = []
        for r in p["records"]:
            if r["kind"] == "wheel" and rng.random() < 0.3:
                extra.append({"kind": "scroll", "t": r["t"] + 1, "x": 0, "y": rng.uniform(0, 4000), "content_height": 5200, "view_height": 880, "trusted": True, "target": 6})
            elif r["kind"] == "pointerdown" and rng.random() < 0.8:
                r.update(box_x=round(r["x"] - 30), box_y=round(r["y"] - 12), box_w=60, box_h=24)
            elif r["kind"] == "keydown" and rng.random() < 0.02:
                extra.append({"kind": "input", "t": r["t"] - 5, "field": "text", "via": rng.choice(["paste", "fill"]), "trusted": True, "target": 5})
        records = sorted(p["records"] + extra, key=lambda r: r["t"])
        # Away from the tab in some of the longer pauses: the boundary when it left, the return with how long.
        for before, after in zip(records, records[1:]):
            if after["t"] - before["t"] > 600 and rng.random() < 0.5:
                extra += [{"kind": "boundary", "t": before["t"] + 50, "reason": "hidden"},
                          {"kind": "visible", "t": after["t"] - 50, "hidden_ms": after["t"] - before["t"] - 100}]
        p["records"] = sorted(p["records"] + extra, key=lambda r: r["t"])
    return rows


class OutcomeAndBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, cls.truth = generate(people=16, sessions_per_person=3, seed=11)
        rows = with_signals(rows)
        # Goal events: the session's final segment decides, so behaviour before the goal carries the planted state.
        final = {}
        for seg in cls.truth:
            if seg["session_id"] not in final or seg["end_ms"] > final[seg["session_id"]]["end_ms"]:
                final[seg["session_id"]] = seg
        origins = {r["properties"]["$session_id"]: r["properties"]["time_origin_ms"] for r in rows if r["event"] == "orion_input"}
        people = {r["properties"]["$session_id"]: r["person_id"] for r in rows if r["event"] == "orion_input"}
        cls.goals = {}
        for i, (session, seg) in enumerate(sorted(final.items())):
            if seg["state"] != "calm":
                continue
            at = origins[session] + seg["end_ms"] + 500
            cls.goals[session] = at
            event, path = ("order completed", "/checkout") if i % 2 else ("$pageview", "/checkout/done")
            rows.append({"uuid": f"goal-{session}", "event": event, "timestamp": datetime.fromtimestamp(at / 1000, tz=timezone.utc).isoformat(),
                         "distinct_id": people[session], "person_id": people[session], "properties": {"$session_id": session, "$pathname": path}})
        cls.fake = FakePostHog(rows)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "recipes").mkdir()
        cls.recipe_path = cls.root / "recipes" / "checkout-outcome.yaml"
        cls.recipe_path.write_text(RECIPE)
        cls.orion = Orion(posthog_project="1", host=cls.fake.url, api_key="phx_test", home=cls.root / ".orion", log=lambda *a: None)
        cls.orion.posthog.sleep = lambda s: None
        cls.orion.posthog.poll_seconds = 0
        cls.orion.pull("30d", recipe=cls.recipe_path)
        cls.clf = cls.orion.train(cls.recipe_path)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        cls.tmp.cleanup()

    def test_pull_asks_for_the_goal_events(self):
        include = next(r[2]["include"] for r in self.fake.requests if r[0] == "POST" and r[1].endswith("file_download_batch_exports/"))
        self.assertIn("order completed", include)
        self.assertIn("$pageview", include)

    def test_outcome_labels_end_before_the_goal(self):
        labels = read_jsonl(self.root / ".orion" / "labels" / "checkout-outcome" / "outcome.jsonl")
        recordings = {r["recorder_id"]: r for r in self.orion.store.recordings()}
        by_session = {}
        for label in labels:
            by_session.setdefault(label["session_id"], []).append(label)
            self.assertEqual(label["source"], "outcome")
            self.assertAlmostEqual(label["end_ms"] - label["start_ms"], 10000)
        self.assertGreater(len(labels), 60)
        self.assertTrue(all(len(v) <= 4 for v in by_session.values()))
        for session, found in by_session.items():
            purchased = session in self.goals
            self.assertTrue(all(l["targets"]["outcome"] == ("purchased" if purchased else "left") for l in found))
            if purchased:
                clock = Clock(recordings[found[0]["recorder_id"]])
                goal = clock.to_recorder(self.goals[session])
                self.assertTrue(all(l["end_ms"] <= goal - 5000 + 1 and l["start_ms"] >= goal - 120000 - 1 for l in found))
        self.assertTrue(any(l["goal"] == "/checkout/done" for l in labels) and any(l["goal"] == "order completed" for l in labels))

    def test_the_dataset_has_the_new_features_and_baselines(self):
        manifest = json.loads((self.root / ".orion" / "datasets" / "checkout-outcome" / "v1" / "manifest.json").read_text())
        names = set(manifest["features"])
        for name in ("paste_count", "autofill_count", "hidden_count", "hidden_ms", "scroll_depth_max", "overshoot_fraction",
                     "overshoot_px_median", "key_hold_ms_median_rel", "pointer_speed_px_s_median_rel"):
            self.assertIn(name, names)
        self.assertTrue(manifest["recipe"]["quality"]["allow_away"])
        rows = [json.loads(line) for split in ("train", "validation", "test")
                for line in (self.root / ".orion" / "datasets" / "checkout-outcome" / "v1" / f"{split}.jsonl").read_text().splitlines()]
        with_history = [r for r in rows if r["features"]["key_hold_ms_median_rel"] is not None]
        # A person's first page load has no history; later ones do.
        self.assertTrue(0 < len(with_history) < len(rows))
        self.assertTrue(any(r["features"]["scroll_depth_max"] for r in rows))

    def test_the_model_carries_its_baseline_and_deploys(self):
        spec = self.clf.spec
        self.assertEqual(spec["features"]["baseline"], {"tile_ms": 10000, "grace_ms": 1000, "cap": 100, "min_tiles": 3})
        self.assertTrue(spec["window"]["allow_away"])
        deployed = self.clf.deploy(out=self.root / "public" / "orion")
        self.assertIn("mask: 'timing'", deployed["snippet"])
        # The browser runtime scores it, new records and _rel features included (no stored history in a fresh browser).
        sessions = {}
        for row in self.orion.store.iter_telemetry():
            p = row["properties"]
            sessions.setdefault(p["$session_id"], []).extend(decode_records(p))
        payload = [{"session_id": s, "records": sorted(r, key=lambda x: x["t"])} for s, r in list(sessions.items())[:4]]
        (self.root / "sessions.json").write_text(json.dumps(payload))
        out = subprocess.run(["node", Path(__file__).parent / "live_sim.mjs", deployed["file"], self.root / "sessions.json"],
                             check=True, capture_output=True, text=True)
        result = json.loads(out.stdout)
        labels = [t["label"] for s in result["sessions"] for t in s["ticks"] if t["label"]]
        self.assertGreater(len(labels), 20)
        self.assertTrue(set(labels) <= {"purchased", "left"})
        self.assertEqual(sum(s["stats"]["errors"] for s in result["sessions"]), 0)

    def test_corrections_deploy_with_the_types_mask(self):
        from paradigm_orion.deploy import deploy
        path = self.clf.path / "model.json"
        original = path.read_text()
        spec = json.loads(original)
        spec["features"]["families"].append("corrections")
        try:
            path.write_text(json.dumps(spec))
            _, _, snippet, _ = deploy(self.orion.workspace, spec["name"], self.root / "public" / "orion2", spec["version"])
        finally:
            path.write_text(original)
        self.assertIn("mask: 'types'", snippet)


if __name__ == "__main__":
    unittest.main()
