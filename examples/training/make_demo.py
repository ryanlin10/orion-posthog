"""Generate explicitly synthetic data to exercise recipes, never to validate sentiment prediction."""
import json
from pathlib import Path
import sys

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts/training-demo-input")
out.mkdir(parents=True, exist_ok=False)
batches, labels, recordings = [], [], []
for person in range(12):
    recorder = f"demo-{person}"
    recordings.append({"recorder_id": recorder, "group_id": f"person-{person}", "subject_id": f"person-{person}"})
    for i, sentiment in enumerate(("negative", "neutral", "positive")):
        start = i * 10000
        records = []
        for j in range(10):
            t = start + j * 700 + 10
            records.extend([{"kind": "keydown", "t": t, "press_id": i * 10 + j, "repeat": False, "trusted": False},
                            {"kind": "keyup", "t": t + 50 + person * 2, "press_id": i * 10 + j, "trusted": False}])
        batches.append({"schema_version": "1.2", "recorder_id": recorder, "batch_sequence": i,
                        "time_origin_ms": 1700000000000, "quality": {"dropped_before_batch": 0},
                        "capture": {"synthetic_allowed": True}, "records": records})
        labels.append({"id": f"{recorder}-{i}", "recorder_id": recorder, "timebase": "recorder_ms",
                       "start_ms": start, "end_ms": start + 10000, "source": "synthetic", "version": "demo-v1",
                       "confidence": 1, "targets": {"sentiment": sentiment}})
for name, values in (("batches", batches), ("labels", labels), ("recordings", recordings)):
    (out / f"{name}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in values))
recipe = {"version": 1, "targets": {"sentiment": {"type": "categorical", "classes": ["negative", "neutral", "positive"]}},
          "labels": {"sources": ["synthetic"]}, "quality": {"allow_synthetic": True},
          "split": {"assignments": {f"person-{p}": "train" if p < 8 else "validation" if p < 10 else "test" for p in range(12)}}}
(out / "recipe.json").write_text(json.dumps(recipe, indent=2) + "\n")
print(out)
