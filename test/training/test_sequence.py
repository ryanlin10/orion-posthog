"""Sequence models: the browser's time-step encoding matches Python's, and a transformer trained on generated
data runs in onnxruntime-web with the same labels."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from paradigm_orion.sequence import CHANNELS, channels_for, encode_steps

ROOT = Path(__file__).resolve().parents[2]
HAS_TORCH = importlib.util.find_spec("torch") is not None
RUNNER = """
import { readFileSync } from 'node:fs';
import { encodeSteps } from '%s';
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify(cases.map(c => Array.from(encodeSteps(c.events, c.duration, c.steps, c.step, c.channels)))));
""" % (ROOT / "src" / "sequence.js").as_uri()


def random_events(seed, duration):
    rng = np.random.default_rng(seed)
    events, t, press, x, y = [], 0.0, 1, 300.0, 200.0
    held = []
    while t < duration:
        p = rng.random()
        if p < 0.5:
            x += rng.normal(scale=8); y += rng.normal(scale=6)
            events.append({"kind": "pointermove", "t": t, "x": x, "y": y, "pointer_id": 1})
        elif p < 0.6:
            events.append({"kind": "keydown", "t": t, "press_id": press, "repeat": bool(rng.random() < 0.05)}); held.append((press, t + rng.uniform(40, 150))); press += 1
        elif p < 0.68:
            events.append({"kind": "pointerdown", "t": t, "press_id": press, "pointer_id": 1}); held.append((press, t + rng.uniform(50, 120), "ptr")); press += 1
        elif p < 0.75:
            events.append({"kind": "wheel", "t": t})
        elif p < 0.78:
            events.append({"kind": rng.choice(["focusin", "focusout"]), "t": t})
        elif p < 0.785:
            events.append({"kind": "boundary", "t": t})
        for h in list(held):
            if h[1] <= t:
                events.append({"kind": "pointerup" if len(h) == 3 else "keyup", "t": h[1], "press_id": h[0], "pointer_id": 1})
                held.remove(h)
        t += float(rng.choice([rng.uniform(5, 12), rng.uniform(20, 300), rng.uniform(150, 600)]))
    return sorted(events, key=lambda e: e["t"])


class EncodingParityTests(unittest.TestCase):
    def test_browser_and_python_steps_match(self):
        families = (["pointer", "keyboard", "scroll", "focus"], ["pointer"], ["keyboard", "scroll"], ["keyboard", "focus"])
        cases = [{"events": random_events(seed, duration), "duration": duration, "steps": 200, "step": 50, "channels": channels_for(f)}
                 for (seed, duration), f in zip(((1, 10000), (2, 7300), (3, 10000), (4, 4100)), families)]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "cases.json").write_text(json.dumps(cases))
            (tmp / "run.mjs").write_text(RUNNER)
            got = json.loads(subprocess.run(["node", tmp / "run.mjs", tmp / "cases.json"], check=True, capture_output=True, text=True).stdout)
        for case, js in zip(cases, got):
            expected = encode_steps(case["events"], case["duration"], case["steps"], case["step"], case["channels"])
            self.assertEqual(expected.shape, (case["steps"], len(case["channels"])))
            self.assertGreater(expected[:, case["channels"].index("active")].sum(), 0)
            np.testing.assert_array_equal(np.array(js, dtype=np.float32).reshape(expected.shape), expected)
        self.assertEqual(len(CHANNELS), 14)

    def test_unchosen_families_leave_no_trace(self):
        events = random_events(5, 10000)
        pointer_only = [e for e in events if e["kind"] not in ("keydown", "keyup", "wheel", "focusin", "focusout")]
        channels = channels_for(["pointer"])
        self.assertEqual(channels, ["moves", "path", "peak_speed", "turning", "pointer_downs", "pointer_ups", "click_hold", "gap", "active"])
        np.testing.assert_array_equal(encode_steps(events, 10000, 100, 100, channels), encode_steps(pointer_only, 10000, 100, 100, channels))
        full = encode_steps(events, 10000, 100, 100)
        np.testing.assert_array_equal(full, encode_steps(events, 10000, 100, 100, CHANNELS))
        self.assertEqual(full.shape, (100, 14))


@unittest.skipUnless(HAS_TORCH, "Install paradigm-orion[sequence] for sequence model tests")
class SequenceModelTests(unittest.TestCase):
    def test_tcn_trains_exports_and_labels_live_in_onnxruntime_web(self):
        import test_end_to_end as e2e
        from fakes import FakePostHog, ScriptedModel
        from paradigm_orion import Orion
        from paradigm_orion.synthetic import generate
        from paradigm_orion.train import load_dataset
        rows, truth = generate(people=24, sessions_per_person=3, seed=9)
        fake = FakePostHog(rows)
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            recipe = tmp / "r.yaml"
            recipe.write_text(e2e.RECIPE.replace("architecture: boosted_trees\n  trees: 150", "architecture: tcn\n  epochs: 12\n  hidden: 32")
                              .replace("telemetry: [pointer, keyboard, scroll, focus]", "telemetry: [pointer, keyboard]"))
            o = Orion(posthog_project="1", host=fake.url, api_key="phx_test", home=tmp / ".orion", llm=ScriptedModel(), log=lambda *a: None)
            o.posthog.sleep, o.posthog.poll_seconds = (lambda s: None), 0
            o.pull("30d", recipe=recipe)
            clf = o.train(recipe, shuffles=1)
            self.assertEqual(clf.spec["estimator"]["kind"], "onnx")
            self.assertEqual(clf.spec["estimator"]["channels"], channels_for(["pointer", "keyboard"]))
            curve = clf.report["curve"]["epochs"]
            self.assertTrue(curve and all(e["train"] > 0 and e["validation"] > 0 for e in curve))
            self.assertGreater(clf.report["metrics"]["validation"]["balanced_accuracy"], 0.7)
            deployed = Path(clf.deploy(out=tmp / "public")["file"])
            onnx = deployed.parent / clf.spec["estimator"]["file"]
            self.assertTrue(onnx.exists())

            # The browser's onnxruntime-web gives the probabilities PyTorch gives.
            _, data = load_dataset(tmp / ".orion" / "datasets" / "checkout-sentiment" / "v1")
            spec = clf.spec
            windows = data["validation"][:12]
            steps = [encode_steps(r["sequence"], r["end_ms"] - r["start_ms"], spec["estimator"]["steps"], spec["estimator"]["step_ms"], spec["estimator"]["channels"]) for r in windows]
            (tmp / "cases.json").write_text(json.dumps([s.ravel().tolist() for s in steps]))
            got = json.loads(subprocess.run(["node", ROOT / "test" / "training" / "onnx_predict.mjs", deployed, onnx, tmp / "cases.json"],
                                            check=True, capture_output=True, text=True).stdout)
            import onnxruntime
            session = onnxruntime.InferenceSession(str(onnx), providers=["CPUExecutionProvider"])
            logits = session.run(None, {"steps": np.stack(steps)})[0]
            classes = spec["estimator"]["classes"]
            p = 1 / (1 + np.exp(-logits[:, 0]))
            np.testing.assert_allclose([[g["probabilities"][c] for c in classes] for g in got], np.stack([1 - p, p], axis=1), rtol=1e-4, atol=1e-5)

            # Live labels on held-out people follow the planted state.
            manifest = json.loads((tmp / ".orion" / "datasets" / "checkout-sentiment" / "v1" / "manifest.json").read_text())
            held = {g for g, s in manifest["groups"].items() if s != "train"}
            sessions = {}
            for row in rows:
                if row["event"] == "orion_input" and row["person_id"] in held:
                    sessions.setdefault(row["properties"]["$session_id"], []).extend(row["properties"]["records"])
            (tmp / "sessions.json").write_text(json.dumps([{"session_id": s, "records": sorted(v, key=lambda r: r["t"])} for s, v in sessions.items()]))
            out = json.loads(subprocess.run(["node", ROOT / "test" / "training" / "live_sim.mjs", deployed, tmp / "sessions.json"],
                                            check=True, capture_output=True, text=True).stdout)
            right = total = 0
            for s in out["sessions"]:
                self.assertEqual(s["stats"]["errors"], 0)
                for tick in s["ticks"]:
                    state = e2e.state_at(truth, s["session_id"], tick["t"] - 5000)
                    if tick["label"] and state:
                        total += 1
                        right += tick["label"] == state
            self.assertGreater(total, 200)
            self.assertGreater(right / total, 0.7)
