"""Small explicit recipe contract: reject typos rather than silently changing an experiment."""
import copy
import math

DEFAULT = {
    "version": 1,
    "window": {"mode": "label", "size_ms": 10000, "stride_ms": 10000, "max_events": 100000, "max_duration_ms": 300000, "max_windows_per_label": 10000},
    "labels": {"sources": ["human", "self_report"], "min_confidence": 0.0},
    # allow_away keeps windows in which the person left the tab or window (gaps named hidden, blur or pagehide).
    "quality": {"min_events": 2, "allow_gaps": False, "allow_away": False, "allow_synthetic": False, "require_quality_metadata": False},
    # baseline: None, or {tile_ms, grace_ms, cap, min_tiles} to add <feature>_rel against the person's earlier page loads.
    "features": {"families": ["keyboard", "pointer", "scroll"], "exclude": [], "hook": None, "baseline": None},
    "split": {"seed": "telemetry-v1", "fractions": {"train": 0.7, "validation": 0.15, "test": 0.15}, "assignments": {}, "allow_subject_overlap": False},
    "output": {"include_sequence": False},
}


FAMILIES = {"keyboard", "pointer", "scroll", "focus", "volume", "fills", "visibility", "depth", "overshoot", "corrections", "typing", "motion", "touch"}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def recipe_config(value):
    if not isinstance(value, dict) or not isinstance(value.get("targets"), dict) or not value["targets"]:
        raise ValueError("Recipe requires a nonempty targets object")
    if set(value) - (set(DEFAULT) | {"targets", "name"}):
        raise ValueError("Unknown recipe option")
    out = copy.deepcopy(DEFAULT)
    for key, supplied in value.items():
        if key in DEFAULT and isinstance(DEFAULT[key], dict):
            if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT[key]):
                raise ValueError(f"Unknown or invalid {key} option")
            out[key].update(supplied)
        else:
            out[key] = supplied
    if out["version"] != 1:
        raise ValueError("Unsupported recipe version")
    w = out["window"]
    if w["mode"] not in ("label", "sliding"):
        raise ValueError("window.mode must be label or sliding")
    for key in ("size_ms", "stride_ms", "max_events", "max_duration_ms", "max_windows_per_label"):
        if not isinstance(w[key], int) or isinstance(w[key], bool) or w[key] <= 0:
            raise ValueError(f"window.{key} must be a positive integer")
    if w["mode"] == "sliding" and w["size_ms"] > w["max_duration_ms"]:
        raise ValueError("Window size exceeds max_duration_ms")
    allowed_sources = {"human", "self_report", "outcome", "model", "heuristic", "synthetic"}
    labels = out["labels"]
    if not isinstance(labels["sources"], list) or not labels["sources"] or any(not isinstance(x, str) for x in labels["sources"]) or set(labels["sources"]) - allowed_sources:
        raise ValueError("Invalid label sources")
    if not finite(labels["min_confidence"]) or not 0 <= labels["min_confidence"] <= 1:
        raise ValueError("Invalid min_confidence")
    for name, target in out["targets"].items():
        if not isinstance(name, str) or not name or not isinstance(target, dict) or set(target) - {"type", "classes", "min", "max"}:
            raise ValueError("Invalid target definition")
        if target.get("type") not in ("categorical", "multilabel", "continuous"):
            raise ValueError(f"Invalid type for target {name}")
        if target["type"] in ("categorical", "multilabel"):
            classes = target.get("classes")
            if not isinstance(classes, list) or not classes or not all(isinstance(c, str) and c for c in classes) or len(set(classes)) != len(classes):
                raise ValueError(f"Target {name} needs unique string classes")
        else:
            if not finite(target.get("min")) or not finite(target.get("max")) or target["min"] >= target["max"]:
                raise ValueError(f"Target {name} needs finite min < max")
    q = out["quality"]
    if not isinstance(q["min_events"], int) or isinstance(q["min_events"], bool) or q["min_events"] < 1:
        raise ValueError("min_events must be positive")
    for key in ("allow_gaps", "allow_away", "allow_synthetic", "require_quality_metadata"):
        if not isinstance(q[key], bool):
            raise ValueError(f"quality.{key} must be boolean")
    if not isinstance(out["output"]["include_sequence"], bool):
        raise ValueError("include_sequence must be boolean")
    f = out["features"]
    if not isinstance(f["families"], list) or any(not isinstance(x, str) for x in f["families"]) or set(f["families"]) - FAMILIES:
        raise ValueError("Invalid feature families")
    if f["baseline"] is not None:
        b = f["baseline"]
        if not isinstance(b, dict) or set(b) != {"tile_ms", "grace_ms", "cap", "min_tiles"} or any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in b.values()):
            raise ValueError("features.baseline needs positive integers tile_ms, grace_ms, cap and min_tiles")
    if f["hook"] is not None and not isinstance(f["hook"], str):
        raise ValueError("features.hook must be a local Python file")
    if not isinstance(f["exclude"], list) or any(not isinstance(k, str) for k in f["exclude"]):
        raise ValueError("features.exclude must be a list of feature names")
    fractions = out["split"]["fractions"]
    if not isinstance(fractions, dict) or set(fractions) != {"train", "validation", "test"} or any(not finite(x) or x < 0 for x in fractions.values()) or not math.isclose(sum(fractions.values()), 1):
        raise ValueError("Split fractions must be nonnegative and sum to 1")
    assignments = out["split"]["assignments"]
    if not isinstance(assignments, dict) or any(not isinstance(k, str) or not isinstance(v, str) or v not in fractions for k, v in assignments.items()):
        raise ValueError("Invalid explicit split assignments")
    if not isinstance(out["split"]["seed"], str):
        raise ValueError("Split seed must be a string")
    if not isinstance(out["split"]["allow_subject_overlap"], bool):
        raise ValueError("allow_subject_overlap must be boolean")
    return out
