import json
from pathlib import Path
import tempfile
import unittest

from paradigm_orion.dataset.pipeline import build_dataset, decode_records
from paradigm_orion.dataset.config import recipe_config


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recipe = {"version": 1, "targets": {"mood": {"type": "categorical", "classes": ["a", "b"]}},
                       "split": {"assignments": {"person-a": "train", "person-b": "test"}}, "output": {"include_sequence": True}}
        self.metadata = [{"recorder_id": "r1", "group_id": "person-a", "subject_id": "a"}, {"recorder_id": "r2", "group_id": "person-b", "subject_id": "b"}]
        self.labels = [dict(id=r, recorder_id=r, timebase="recorder_ms", start_ms=0, end_ms=100, source="human", version="v1", confidence=1, targets={"mood": "a" if r == "r1" else "b"}) for r in ("r1", "r2")]
        self.batches = [self.batch(r, 0, [{"kind": "keydown", "t": 10.1, "code": "KeyA", "key": "SECRET", "modifiers": 4, "trusted": True},
                                        {"kind": "keyup", "t": 30.3, "code": "KeyA", "trusted": True}]) for r in ("r1", "r2")]

    def batch(self, recorder, seq, rows):
        return {"schema_version": "1.2", "recorder_id": recorder, "batch_sequence": seq, "time_origin_ms": 1700000000000,
                "quality": {"dropped_before_batch": 0}, "records": rows}

    def write(self, name, rows):
        p = self.root / name
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return p

    def run_build(self, name="result"):
        rp = self.root / "recipe.json"; rp.write_text(json.dumps(self.recipe))
        return build_dataset(rp, [self.write("batches.jsonl", self.batches)], self.write("labels.jsonl", self.labels), self.write("recordings.jsonl", self.metadata), self.root / name)

    def examples(self, split="train", name="result"):
        return [json.loads(x) for x in (self.root / name / f"{split}.jsonl").read_text().splitlines()]

    def test_basic_privacy_precision_and_provenance(self):
        manifest = self.run_build()
        row = self.examples()[0]
        self.assertAlmostEqual(row["features"]["key_hold_ms_median"], 20.2)
        self.assertEqual(row["sequence"][0]["press_id"], row["sequence"][1]["press_id"])
        self.assertNotIn("SECRET", json.dumps(row)); self.assertNotIn("KeyA", json.dumps(row)); self.assertNotIn("modifiers", json.dumps(row))
        self.assertEqual(row["label"]["source"], "human")
        self.assertEqual(manifest["counts"]["train_examples"], 1)
        self.assertEqual(manifest["counts"]["test_examples"], 1)

    def test_group_isolation_and_deterministic_output(self):
        a = self.run_build(); b = self.run_build("again")
        self.assertEqual(a, b)
        self.assertTrue({r["group_id"] for r in self.examples()}.isdisjoint({r["group_id"] for r in self.examples("test")}))
        for name in ("train", "test", "validation", "manifest"):
            ext = "json" if name == "manifest" else "jsonl"
            self.assertEqual((self.root / "result" / f"{name}.{ext}").read_bytes(), (self.root / "again" / f"{name}.{ext}").read_bytes())

    def test_reorders_and_deduplicates_batches(self):
        first = self.batches[0]
        self.batches = [self.batch("r1", 1, [{"kind": "pointermove", "t": 80}]), first, first, self.batches[1]]
        m = self.run_build()
        self.assertEqual(m["counts"]["duplicate_batches"], 1)
        self.assertEqual(len(self.examples()[0]["sequence"]), 3)

    def test_conflicting_duplicate_fails_without_output(self):
        self.batches.append(self.batch("r1", 0, [{"kind": "pointermove", "t": 5}]))
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.run_build()
        self.assertFalse((self.root / "result").exists())

    def test_gap_rejection_and_opt_in_keeps_pairing_broken(self):
        self.batches[0] = self.batch("r1", 0, [{"kind": "keydown", "t": 10, "code": "KeyA"}])
        self.batches.append(self.batch("r1", 2, [{"kind": "keyup", "t": 30, "code": "KeyA"}]))
        m = self.run_build()
        self.assertEqual(m["rejections"], {"capture-gap": 1})
        self.recipe["quality"] = {"allow_gaps": True}
        self.run_build("allowed")
        row = self.examples(name="allowed")[0]
        self.assertIsNone(row["features"]["key_hold_ms_median"])
        self.assertIn("missing-batch", row["quality"]["gap_reasons"])

    def test_overlap_and_clock_ambiguity_are_rejected(self):
        self.labels.append({**self.labels[0], "id": "overlap", "start_ms": 90, "end_ms": 110})
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.run_build()
        self.labels.pop(); self.labels[0]["timebase"] = "epoch_ms"
        with self.assertRaisesRegex(ValueError, "timebase"):
            self.run_build()

    def test_model_labels_are_not_silently_treated_as_gold(self):
        self.labels[0]["source"] = "model"
        m = self.run_build()
        self.assertEqual(m["rejections"], {"label-policy": 1})
        self.recipe["labels"] = {"sources": ["human", "model"]}
        self.run_build("proxy")
        self.assertEqual(self.examples(name="proxy")[0]["label"]["source"], "model")

    def test_no_neutral_label_is_invented(self):
        del self.labels[0]["targets"]["mood"]
        with self.assertRaisesRegex(ValueError, "missing target"):
            self.run_build()

    def test_multilabel_regression_and_sliding_windows(self):
        self.recipe["targets"] = {"states": {"type": "multilabel", "classes": ["calm", "tense"]}, "valence": {"type": "continuous", "min": -1, "max": 1}}
        self.recipe["window"] = {"mode": "sliding", "size_ms": 40, "stride_ms": 20}
        for l in self.labels: l["targets"] = {"states": ["calm"], "valence": 0.3}
        self.run_build()
        row = self.examples()[0]
        self.assertEqual(row["end_ms"], 40); self.assertEqual(row["targets"]["states"], ["calm"])
        self.assertEqual(row["targets"]["valence"], 0.3)

    def test_subject_leakage_fails(self):
        self.metadata[1]["subject_id"] = "a"
        with self.assertRaisesRegex(ValueError, "crosses groups"):
            self.run_build()

    def test_synthetic_and_missing_quality_filters(self):
        self.batches[0]["records"][0]["trusted"] = False
        del self.batches[1]["quality"]
        self.recipe["quality"] = {"require_quality_metadata": True}
        m = self.run_build()
        self.assertEqual(m["rejections"], {"synthetic-input": 1, "unknown-quality": 1})

    def test_feature_hooks_are_sanitized_and_hashed(self):
        (self.root / "hook.py").write_text("def extract(events, context):\n    assert all('code' not in e for e in events)\n    assert set(context) == {'start_ms','end_ms'}\n    return {'custom': 1}\n")
        self.recipe["features"] = {"hook": "hook.py"}
        m = self.run_build()
        self.assertEqual(self.examples()[0]["features"]["custom"], 1)
        self.assertEqual(len(m["provenance"]["feature_hook"]), 64)

    def test_half_open_window_excludes_future_input(self):
        self.batches[0]["records"].append({"kind": "pointermove", "t": 100, "x": 999})
        self.run_build()
        self.assertEqual(len(self.examples()[0]["sequence"]), 2)

    def test_existing_output_is_not_overwritten(self):
        self.run_build()
        with self.assertRaisesRegex(ValueError, "already exists"): self.run_build()

    def test_unknown_recipe_and_nonfinite_labels_fail(self):
        self.recipe["window"] = {"strdie_ms": 10}
        with self.assertRaisesRegex(ValueError, "Unknown"): self.run_build()
        del self.recipe["window"]; self.labels[0]["start_ms"] = float("nan")
        with self.assertRaisesRegex(ValueError, "invalid JSON"): self.run_build()

    def test_column_decoder_preserves_null_absence_and_precision(self):
        rows = decode_records({"record_columns": {"length": 2, "fields": {"kind": ["keydown", "keyup"], "t": [0.1, 0.3], "extra": [None, None]}, "present": {"extra": [0]}}})
        self.assertIn("extra", rows[0]); self.assertNotIn("extra", rows[1])
        self.assertEqual(rows[1]["t"] - rows[0]["t"], 0.3 - 0.1)

    def test_hash_split_stays_stable_when_new_groups_arrive(self):
        self.recipe["split"]["assignments"] = {}
        before = self.run_build()
        self.metadata.append({"recorder_id":"new-recording", "group_id":"new-person"})
        after = self.run_build("expanded")
        for group, split in before["groups"].items():
            self.assertEqual(after["groups"][group], split)
        self.assertEqual(before["output_sha256"], after["output_sha256"])

    def test_filtered_proxy_annotation_can_coexist_with_human_annotation(self):
        self.labels.append({**self.labels[0], "id": "proxy", "source": "model"})
        m = self.run_build()
        self.assertEqual(m["counts"]["train_examples"], 1)
        self.assertEqual(m["rejections"], {"label-policy": 1})

    def test_temporal_gap_rejects_window_even_before_next_sample_arrives(self):
        self.batches[0] = self.batch("r1", 0, [{"kind":"pointermove", "t":10}, {"kind":"pointermove", "t":15}])
        self.batches.append(self.batch("r1", 2, [{"kind":"pointermove", "t":200}]))
        m = self.run_build()
        self.assertEqual(m["rejections"], {"capture-gap":1})

    def test_clock_jumps_and_cumulative_loss_are_reported(self):
        self.batches[0]["clock"] = {"epoch_ms": 10000, "perf_ms": 40}
        extra = self.batch("r1", 1, [{"kind":"pointermove", "t":80}])
        extra["clock"] = {"epoch_ms": 20000, "perf_ms": 90}
        extra["quality"]["dropped_before_batch"] = 3
        self.batches.append(extra)
        self.recipe["quality"] = {"allow_gaps":True}
        self.run_build()
        self.assertEqual(self.examples()[0]["quality"]["gap_reasons"], ["clock-discontinuity", "reported-loss"])

    def test_within_person_protocol_requires_explicit_opt_in(self):
        self.metadata[1]["subject_id"] = "a"
        self.recipe["split"]["allow_subject_overlap"] = True
        m = self.run_build()
        self.assertTrue(any("Within-person" in w for w in m["warnings"]))

    def test_large_window_is_rejected_without_unbounded_loading(self):
        self.recipe["window"] = {"max_events":1}
        m = self.run_build()
        self.assertEqual(m["rejections"], {"too-many-events":2})

    def test_feature_ablation_and_schema_validation(self):
        self.recipe["features"] = {"exclude":["key_interval_ms_median"]}
        self.run_build()
        self.assertNotIn("key_interval_ms_median", self.examples()[0]["features"])
        self.recipe["features"]["exclude"] = ["typo"]
        with self.assertRaisesRegex(ValueError, "unknown feature"):
            self.run_build("bad")

    def test_sample_limit_and_missing_batches_have_distinct_outcomes(self):
        self.recipe["window"] = {"mode":"sliding", "size_ms":1, "stride_ms":1, "max_windows_per_label":10}
        with self.assertRaisesRegex(ValueError,"max_windows_per_label"):
            self.run_build()

    def test_quality_and_capture_metadata_are_validated(self):
        self.batches[0]["capture"] = None
        with self.assertRaisesRegex(ValueError, "capture metadata"):
            self.run_build()
        self.batches[0]["capture"] = {}; self.batches[0]["quality"]["dropped_before_batch"] = -1
        with self.assertRaisesRegex(ValueError, "quality metadata"):
            self.run_build()


    def test_pointer_holds_pair_buttons_and_cancel_without_exporting_identity(self):
        self.batches[0]["records"] = [
            {"kind":"pointerdown", "t":1, "pointer_id":1, "button":0},
            {"kind":"pointerdown", "t":2, "pointer_id":1, "button":2},
            {"kind":"pointerup", "t":11, "pointer_id":1, "button":0},
            {"kind":"pointercancel", "t":12, "pointer_id":1},
            {"kind":"pointerup", "t":22, "pointer_id":1, "button":2},
        ]
        self.run_build()
        row = self.examples()[0]
        self.assertEqual(row["features"]["click_hold_ms_median"], 10)
        self.assertEqual(row["quality"]["matched_pointer_pairs"], 1)
        self.assertTrue(all("button" not in event for event in row["sequence"]))

    def test_malformed_pointer_identity_fails_before_pairing(self):
        self.batches[0]["records"] = [{"kind":"pointerdown", "t":1, "pointer_id":[]}]
        with self.assertRaisesRegex(ValueError, "numeric telemetry"):
            self.run_build()

    def test_short_sliding_annotation_is_reported_not_silently_lost(self):
        self.recipe["window"] = {"mode":"sliding", "size_ms":101}
        manifest = self.run_build()
        self.assertEqual(manifest["rejections"], {"annotation-too-short":2})

    def test_unhashable_recipe_values_produce_validation_errors(self):
        for section, value in (("features", {"families":[{}]}), ("labels", {"sources":[[]]}), ("split", {"assignments":{"g":[]}})):
            with self.subTest(section=section), self.assertRaises(ValueError):
                recipe_config({**self.recipe, section:value})


if __name__ == "__main__":
    unittest.main()
