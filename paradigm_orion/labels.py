"""Labels shared by every labeller: target checks, label files and merging.

A label is an interval of one recorder's time (`timebase: recorder_ms`, half-open [start_ms, end_ms)) with a
source, a version naming the labeller and model, a confidence and the target value. When labellers disagree
about overlapping time, people's own answers win, then human labellers, then text labels, then replay labels.
"""
import hashlib
import json

from .workspace import read_jsonl, write_jsonl

PRIORITY = {"self_report": 0, "human": 1, "text": 2, "replay": 3}


def describe_target(target):
    if target["type"] == "continuous":
        return f'"{target["name"]}": a number from {target["min"]:g} to {target["max"]:g}'
    if target.get("binary"):
        return f'"{target["name"]}": true or false'
    return f'"{target["name"]}": one of {", ".join(target["classes"])}'


def coerce(target, value):
    """A labeller's answer as the builder's value, or None when it is missing or invalid."""
    if value is None:
        return None
    if target["type"] == "continuous":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if target["min"] <= number <= target["max"] else None
    if target.get("binary"):
        if isinstance(value, bool):
            return "yes" if value else "no"
        return {"true": "yes", "yes": "yes", "false": "no", "no": "no"}.get(str(value).strip().lower())
    text = str(value).strip().lower()
    return next((c for c in target["classes"] if c.lower() == text), None)


def make_label(recipe, labeller, recorder_id, start_ms, end_ms, value, confidence, version, source="model", **extra):
    target = recipe["target"]
    key = json.dumps([recipe["name"], labeller, recorder_id, round(start_ms, 3), round(end_ms, 3)])
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    return {"id": f"{labeller}-{hashlib.sha256(key.encode()).hexdigest()[:20]}", "recorder_id": recorder_id, "timebase": "recorder_ms",
            "start_ms": start_ms, "end_ms": end_ms, "source": source, "version": version,
            "confidence": min(1.0, max(0.0, confidence)), "targets": {target["name"]: value}, "labeller": labeller, **extra}


def label_path(workspace, recipe, labeller):
    return workspace.path("labels", recipe["name"], f"{labeller}.jsonl")


def save_labels(workspace, recipe, labeller, labels):
    write_jsonl(label_path(workspace, recipe, labeller), labels)


def merge(label_lists):
    """One non-overlapping list per recorder; higher-priority labellers win overlaps."""
    ranked = sorted((l for labels in label_lists for l in labels),
                    key=lambda l: (PRIORITY.get(l.get("labeller"), 9), l["recorder_id"], l["start_ms"], l["id"]))
    kept, by_recorder, dropped = [], {}, 0
    for label in ranked:
        spans = by_recorder.setdefault(label["recorder_id"], [])
        if any(label["start_ms"] < b and a < label["end_ms"] for a, b in spans):
            dropped += 1
            continue
        spans.append((label["start_ms"], label["end_ms"]))
        kept.append(label)
    kept.sort(key=lambda l: (l["recorder_id"], l["start_ms"], l["id"]))
    return kept, dropped


def load_all(workspace, recipe):
    return {name: read_jsonl(label_path(workspace, recipe, name)) for name in PRIORITY}
