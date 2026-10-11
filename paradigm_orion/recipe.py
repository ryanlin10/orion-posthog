"""Orion recipes: one YAML file declares the target, window, telemetry, labellers, split and model.

The recipe compiles to the dataset builder's stricter JSON contract (paradigm_orion.dataset.config), so
typos fail loudly instead of quietly changing an experiment.
"""
import copy
import json
import re
from pathlib import Path

ARCHITECTURES = ("logistic", "random_forest", "boosted_trees", "gru", "tcn", "transformer")
SEQUENCE_ARCHITECTURES = ("gru", "tcn", "transformer")
FAMILIES = ("pointer", "keyboard", "scroll", "focus", "volume")
LABELLERS = ("human", "self_report", "model")
READERS = ("text", "replay")
ALIGNS = ("past", "symmetric", "future")
SPLITS = ("user", "day", "session")
TOP_LEVEL = {"name", "source", "target", "window", "telemetry", "labels", "split", "model", "deploy", "quality", "features"}

DEFAULT_LLM = "claude-sonnet-5-5"


class RecipeError(ValueError):
    pass


def duration_ms(value, field):
    """'10s', '500ms', '2m', or a bare number of seconds."""
    if isinstance(value, bool):
        raise RecipeError(f"{field} must be a duration such as 10s")
    if isinstance(value, (int, float)):
        return int(round(value * 1000))
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)\s*", str(value))
    if not match:
        raise RecipeError(f"{field} must be a duration such as 10s, 500ms or 2m")
    number, unit = float(match.group(1)), match.group(2)
    return int(round(number * {"ms": 1, "s": 1000, "m": 60000, "h": 3600000}[unit]))


def days(value, field):
    match = re.fullmatch(r"\s*(\d+)\s*d\s*", str(value))
    if not match:
        raise RecipeError(f"{field} must be a number of days such as 30d")
    return int(match.group(1))


def parse_target(name, spec):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][\w-]*", name):
        raise RecipeError(f"Invalid target name {name!r}")
    if spec == "binary":
        return {"type": "categorical", "classes": ["no", "yes"], "binary": True}
    if isinstance(spec, list):
        if len(spec) < 2:
            raise RecipeError(f"Target {name} needs at least two classes")
        return {"type": "categorical", "classes": [str(c) for c in spec]}
    if isinstance(spec, str):
        match = re.fullmatch(r"\s*score\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)\s*", spec)
        if match:
            return {"type": "continuous", "min": float(match.group(1)), "max": float(match.group(2))}
    if isinstance(spec, dict):
        return copy.deepcopy(spec)
    raise RecipeError(f"Target {name} must be binary, a list of classes, or score(min, max)")


def load_recipe(path):
    path = Path(path)
    text = path.read_text()
    if path.suffix == ".json":
        raw = json.loads(text)
    else:
        import yaml
        raw = yaml.safe_load(text)
    recipe = compile_recipe(raw)
    recipe["path"] = str(path)
    return recipe


def compile_recipe(raw):
    if not isinstance(raw, dict):
        raise RecipeError("A recipe is a mapping of settings")
    unknown = set(raw) - TOP_LEVEL
    if unknown:
        raise RecipeError(f"Unknown recipe setting(s): {', '.join(sorted(unknown))}")
    name = raw.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        raise RecipeError("name must be lowercase letters, digits and dashes, e.g. checkout-sentiment")

    target = raw.get("target")
    if not isinstance(target, dict) or len(target) != 1:
        raise RecipeError("target must define exactly one target, e.g. sentiment: [calm, frustrated]")
    (target_name, target_spec), = target.items()
    target_def = parse_target(target_name, target_spec)

    source = raw.get("source", {}) or {}
    if isinstance(source, str):
        source = {"posthog": source.removeprefix("posthog://")}
    if set(source) - {"posthog", "since", "pages"}:
        raise RecipeError("source accepts posthog, since and pages")
    pages = source.get("pages", [])
    if not isinstance(pages, list) or any(not isinstance(p, str) or not p.startswith("/") for p in pages):
        raise RecipeError("source.pages must be a list of paths starting with /")
    source = {"posthog": str(source.get("posthog", "default")), "since_days": days(source.get("since", "30d"), "source.since"), "pages": pages}

    window = raw.get("window", {}) or {}
    if set(window) - {"length", "align", "stride"}:
        raise RecipeError("window accepts length, align and stride")
    length = duration_ms(window.get("length", "10s"), "window.length")
    stride = duration_ms(window.get("stride", window.get("length", "10s")), "window.stride")
    align = window.get("align", "past")
    if align not in ALIGNS:
        raise RecipeError(f"window.align must be one of {', '.join(ALIGNS)}")
    if not 1000 <= length <= 300000:
        raise RecipeError("window.length must be between 1s and 5m")
    if stride < length:
        raise RecipeError("window.stride must be at least window.length: labelled windows cannot overlap")

    telemetry = raw.get("telemetry", ["pointer", "keyboard", "scroll"])
    if not isinstance(telemetry, list) or not telemetry or set(telemetry) - set(FAMILIES):
        raise RecipeError(f"telemetry must list some of {', '.join(FAMILIES)}")
    features = raw.get("features", {}) or {}
    if set(features) - {"exclude", "hook"}:
        raise RecipeError("features accepts exclude and hook")

    labels = raw.get("labels", {}) or {}
    if set(labels) - {"by", "min_confidence", "model"}:
        raise RecipeError("labels accepts by, min_confidence and model")
    by = labels.get("by", ["human"])
    if not isinstance(by, list) or not by or set(by) - set(LABELLERS):
        raise RecipeError(f"labels.by must list some of {', '.join(LABELLERS)}")
    model_labels = labels.get("model", {}) or {}
    if set(model_labels) - {"reads", "llm", "guide", "text", "max_sessions"}:
        raise RecipeError("labels.model accepts reads, llm, guide, text and max_sessions")
    reads = model_labels.get("reads", ["text"] if "model" in by else [])
    if not isinstance(reads, list) or set(reads) - set(READERS):
        raise RecipeError("labels.model.reads must list text and/or replay")
    if "model" in by and not reads:
        raise RecipeError("labels.by includes model, so labels.model.reads must say what the model reads")
    text = model_labels.get("text", {}) or {}
    if set(text) - {"event", "property", "file"}:
        raise RecipeError("labels.model.text accepts event, property and file")
    min_confidence = labels.get("min_confidence", 0.0)
    if not isinstance(min_confidence, (int, float)) or not 0 <= min_confidence <= 1:
        raise RecipeError("labels.min_confidence must be between 0 and 1")
    labels = {"by": by, "min_confidence": float(min_confidence),
              "model": {"reads": reads, "llm": model_labels.get("llm", DEFAULT_LLM), "guide": model_labels.get("guide", ""),
                        "max_sessions": int(model_labels.get("max_sessions", 500)),
                        "text": {"event": text.get("event", "$ai_generation"), "property": text.get("property", "$ai_input"), "file": text.get("file")}}}

    split = raw.get("split", {}) or {}
    if set(split) - {"by", "train", "validation", "val", "test", "seed"}:
        raise RecipeError("split accepts by, train, validation, test and seed")
    split_by = split.get("by", ["user"])
    if isinstance(split_by, str):
        split_by = [split_by]
    if not split_by or set(split_by) - set(SPLITS) or len(set(split_by)) != len(split_by):
        raise RecipeError("split.by must list user, day and/or session")
    if "session" in split_by and len(split_by) > 1:
        raise RecipeError("split.by session cannot be combined with user or day")
    fractions = {"train": split.get("train", 0.7), "validation": split.get("validation", split.get("val", 0.15)), "test": split.get("test", 0.15)}
    if any(not isinstance(v, (int, float)) or v < 0 for v in fractions.values()) or abs(sum(fractions.values()) - 1) > 1e-6:
        raise RecipeError("split fractions must be non-negative and add up to 1")
    split = {"by": split_by, "fractions": {k: float(v) for k, v in fractions.items()}, "seed": str(split.get("seed", name))}

    model = raw.get("model", {}) or {}
    architecture = model.get("architecture", "boosted_trees")
    if architecture not in ARCHITECTURES:
        raise RecipeError(f"model.architecture must be one of {', '.join(ARCHITECTURES)}")
    params = {k: v for k, v in model.items() if k not in ("architecture", "input")}
    model = {"architecture": architecture, "input": "sequence" if architecture in SEQUENCE_ARCHITECTURES else "window_features", "params": params}

    deploy = raw.get("deploy", {}) or {}
    if set(deploy) - {"live", "context", "every", "heartbeat", "confirm"}:
        raise RecipeError("deploy accepts live, context, every, heartbeat and confirm")
    context = deploy.get("context", [])
    if isinstance(context, str):
        context = [context]
    deploy = {"live": bool(deploy.get("live", True)), "context": [str(c) for c in context],
              "every_ms": duration_ms(deploy.get("every", "2s"), "deploy.every"),
              "heartbeat_ms": duration_ms(deploy.get("heartbeat", "60s"), "deploy.heartbeat"), "confirm": int(deploy.get("confirm", 2))}

    quality = raw.get("quality", {}) or {}
    if set(quality) - {"drop_gaps", "drop_synthetic", "min_events"}:
        raise RecipeError("quality accepts drop_gaps, drop_synthetic and min_events")
    quality = {"drop_gaps": bool(quality.get("drop_gaps", True)), "drop_synthetic": bool(quality.get("drop_synthetic", True)),
               "min_events": int(quality.get("min_events", 2))}

    return {"name": name, "source": source, "target": {"name": target_name, **target_def},
            "window": {"length_ms": length, "stride_ms": stride, "align": align}, "telemetry": telemetry,
            "features": {"exclude": list(features.get("exclude", [])), "hook": features.get("hook")},
            "labels": labels, "split": split, "model": model, "deploy": deploy, "quality": quality}


def builder_recipe(recipe, assignments=None):
    """The dataset builder's JSON recipe. Every labeller writes intervals, so windows come from labels."""
    target = {k: v for k, v in recipe["target"].items() if k in ("type", "classes", "min", "max")}
    # Only a split by user keeps every person in one split; the others are within-person protocols.
    within_person = recipe["split"]["by"] != ["user"]
    return {
        "version": 1, "name": recipe["name"],
        "targets": {recipe["target"]["name"]: target},
        # One extra millisecond absorbs floating-point rounding in window bounds computed from wall-clock times.
        "window": {"mode": "label", "max_duration_ms": recipe["window"]["length_ms"] + 1},
        "labels": {"sources": recipe["labels"]["by"], "min_confidence": recipe["labels"]["min_confidence"]},
        "quality": {"allow_gaps": not recipe["quality"]["drop_gaps"], "allow_synthetic": not recipe["quality"]["drop_synthetic"],
                    "min_events": recipe["quality"]["min_events"]},
        "features": {"families": recipe["telemetry"], "exclude": recipe["features"]["exclude"], "hook": recipe["features"]["hook"]},
        "split": {"seed": recipe["split"]["seed"], "fractions": recipe["split"]["fractions"],
                  "assignments": assignments or {}, "allow_subject_overlap": within_person},
        "output": {"include_sequence": recipe["model"]["input"] == "sequence"},
    }
