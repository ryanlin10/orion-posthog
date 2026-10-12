"""`orion build <recipe>`: label, merge, split and build a versioned dataset from the pulled telemetry."""
import json
import math
import tempfile
import time
from pathlib import Path

from .dataset.pipeline import build_dataset
from .labels import load_all, merge, save_labels
from .posthog import epoch_ms
from .recipe import builder_recipe
from .workspace import write_jsonl


def group_of(recording, split_by):
    person = recording.get("person_id") or recording.get("distinct_id") or recording["recorder_id"]
    if split_by == ["session"]:
        return recording.get("session_id") or recording["recorder_id"]
    if split_by == ["day"]:
        return recording["day"]
    if split_by == ["user"]:
        return str(person)
    return f"{person}|{recording['day']}"


def day_assignments(days, fractions):
    """Earlier days train, later days validate, the latest days test."""
    days = sorted(set(days))
    n = len(days)
    if n < 3:
        return {}
    n_train = max(1, math.floor(n * fractions["train"]))
    n_val = max(1, math.floor(n * fractions["validation"])) if fractions["validation"] else 0
    n_train = min(n_train, n - n_val - 1) if fractions["test"] else n_train
    return {d: "train" if i < n_train else "validation" if i < n_train + n_val else "test" for i, d in enumerate(days)}


def run_labellers(store, recipe, llm=None, relabel=False, log=print):
    existing = load_all(store.ws, recipe)
    stats = {}
    if "self_report" in recipe["labels"]["by"]:
        # Cheap and exact, so always rebuilt from the pulled answers.
        from .labellers.self_report import label_self_report
        labels, stats["self_report"] = label_self_report(store, recipe)
        save_labels(store.ws, recipe, "self_report", labels)
        log(f"  self-report labels: {stats['self_report']}")
    if "model" not in recipe["labels"]["by"]:
        return stats
    for reader in recipe["labels"]["model"]["reads"]:
        if existing[reader] and not relabel:
            log(f"  {reader} labels: using {len(existing[reader])} saved labels (pass --relabel to redo)")
            continue
        if llm is None:
            raise ValueError(f"The recipe asks for AI {reader} labels and none are saved; run `orion init` to choose an AI labeller")
        if reader == "text":
            from .labellers.text import label_text as run
        else:
            from .labellers.replay import label_replay as run
        log(f"  {reader} labels: asking {llm.model}")
        labels, stats[reader] = run(store, recipe, llm, log)
        save_labels(store.ws, recipe, reader, labels)
        log(f"  {reader} labels: {stats[reader]}")
    return stats


def build(store, recipe, llm=None, relabel=False, log=print):
    ws = store.ws
    recordings = {r["recorder_id"]: r for r in store.recordings()}
    if not recordings:
        raise ValueError("No Orion telemetry pulled yet; run `orion pull` first")
    label_stats = run_labellers(store, recipe, llm, relabel, log)
    all_labels = load_all(ws, recipe)
    by = recipe["labels"]["by"]
    wanted = {"self_report": "self_report" in by, "human": "human" in by, "text": "model" in by, "replay": "model" in by}
    merged, overlaps = merge([labels for name, labels in all_labels.items() if wanted[name]])
    pages = tuple(recipe["source"]["pages"])
    cutoff = time.time() * 1000 - recipe["source"]["since_days"] * 86400000

    def in_scope(r):
        return epoch_ms(r["first_ts"]) >= cutoff and (not pages or any(p.startswith(pages) for p in r["pages"] if p))

    merged = [l for l in merged if l["recorder_id"] in recordings and in_scope(recordings[l["recorder_id"]])]
    if not merged:
        raise ValueError("No labels fall on pulled telemetry in scope; check the recipe's source settings or label more windows")
    used = {l["recorder_id"] for l in merged}
    split_by = recipe["split"]["by"]
    rows = []
    for rid in sorted(used):
        r = recordings[rid]
        row = {"recorder_id": rid, "group_id": group_of(r, split_by)}
        if split_by == ["user"]:
            row["subject_id"] = str(r.get("person_id") or r.get("distinct_id") or rid)
            if r.get("session_id"):
                row["session_id"] = r["session_id"]
        rows.append(row)
    assignments = day_assignments([row["group_id"] for row in rows], recipe["split"]["fractions"]) if split_by == ["day"] else {}
    config = builder_recipe(recipe, assignments)
    if config["features"]["hook"]:
        config["features"]["hook"] = str((Path(recipe["path"]).parent / config["features"]["hook"]).resolve())
    version = ws.next_version("datasets", recipe["name"])
    out = ws.path("datasets", recipe["name"], f"v{version}")
    with tempfile.TemporaryDirectory(prefix="orion-build-") as tmp:
        tmp = Path(tmp)
        (tmp / "recipe.json").write_text(json.dumps(config))
        write_jsonl(tmp / "recordings.jsonl", rows)
        write_jsonl(tmp / "labels.jsonl", merged)
        # Only labelled recorders go to the builder: it needs metadata for every recorder it reads.
        write_jsonl(tmp / "telemetry.jsonl", (b for b in store.iter_telemetry() if b["properties"].get("recorder_id") in used))
        manifest = build_dataset(tmp / "recipe.json", [tmp / "telemetry.jsonl"], tmp / "labels.jsonl", tmp / "recordings.jsonl", out)
    summary = {"dataset": str(out), "version": version, "labels": len(merged), "overlapping_labels_dropped": overlaps,
               "label_runs": label_stats, "counts": manifest["counts"], "rejections": manifest["rejections"], "warnings": manifest["warnings"]}
    (out / "orion.json").write_text(json.dumps({"recipe": recipe, **summary}, indent=2) + "\n")
    examples = {s: manifest["counts"].get(f"{s}_examples", 0) for s in ("train", "validation", "test")}
    log(f"Built {recipe['name']} dataset v{version}: {examples['train']} train / {examples['validation']} validation / {examples['test']} test windows"
        + (f"; rejected {sum(manifest['rejections'].values())} ({', '.join(f'{k} {v}' for k, v in manifest['rejections'].items())})" if manifest["rejections"] else ""))
    for warning in manifest["warnings"]:
        log(f"  note: {warning}")
    return out, summary
