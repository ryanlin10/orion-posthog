"""Disk-backed ingestion, strict interval alignment, grouped splits and reproducible exports."""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing

from . import __version__
from .config import finite, recipe_config
from .features import extract_features

TELEMETRY_EVENTS = ("orion_input", "behavioral_burst")
SCHEMAS = ("1.0", "1.1", "1.2", "1.3")
KINDS = {"keydown", "keyup", "pointermove", "pointerdown", "pointerup", "pointercancel", "click", "dblclick", "contextmenu", "wheel", "scroll", "resize", "focusin", "focusout", "boundary"}
NUMERIC_FIELDS = {"x", "y", "page_x", "page_y", "dx", "dy", "dz", "mode", "pressure", "tilt_x", "tilt_y", "twist", "width", "height", "dpr", "pointer_id", "button"}


def dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def fingerprint(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def jsonl(path):
    with Path(path).open("rb") as f:
        for number, line in enumerate(iter(lambda: f.readline(8 * 1024 * 1024 + 1), b""), 1):
            if not line.strip():
                continue
            if len(line) > 8 * 1024 * 1024:
                raise ValueError(f"{Path(path).name}:{number}: JSONL line exceeds 8 MiB")
            try:
                value = json.loads(line, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
                if not isinstance(value, dict):
                    raise ValueError("Expected an object")
                yield value
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{Path(path).name}:{number}: invalid JSON object") from exc


def identifier(value, field):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{field} must be a nonempty string of at most 512 characters")
    return value


def decode_records(batch):
    if isinstance(batch.get("records"), list):
        records = batch["records"]
    else:
        c = batch.get("record_columns", {})
        if not isinstance(c, dict):
            raise ValueError("Invalid column format")
        n = c.get("length")
        if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 10000:
            raise ValueError("Invalid column length")
        if not isinstance(c.get("fields"), dict) or not isinstance(c.get("present"), dict):
            raise ValueError("Invalid column format")
        records = [{} for _ in range(n)]
        for key, values in c["fields"].items():
            if not isinstance(values, list) or len(values) != n:
                raise ValueError("Invalid column values")
            indices = c["present"].get(key, list(range(n)))
            if not isinstance(indices, list) or any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < n for i in indices) or len(set(indices)) != len(indices):
                raise ValueError("Invalid column presence")
            for i in indices:
                records[i][key] = values[i]
    if not records or len(records) > 10000:
        raise ValueError("Each batch must contain 1..10000 records")
    for row in records:
        if not isinstance(row, dict) or not isinstance(row.get("kind"), str) or row["kind"] not in KINDS or not finite(row.get("t")):
            raise ValueError("Invalid event kind or timestamp")
        if any(not finite(row[k]) for k in NUMERIC_FIELDS if k in row):
            raise ValueError("Invalid numeric telemetry field")
        if "press_id" in row and (not isinstance(row["press_id"], int) or isinstance(row["press_id"], bool) or row["press_id"] < 0):
            raise ValueError("Invalid press_id")
        if "code" in row and not isinstance(row["code"], str):
            raise ValueError("Invalid key code")
        if "location" in row and (not isinstance(row["location"], int) or isinstance(row["location"], bool)):
            raise ValueError("Invalid key location")
        for key in ("trusted", "repeat", "composing", "coalesced"):
            if key in row and not isinstance(row[key], bool):
                raise ValueError(f"Invalid {key}")
    return records


def split_for(group, config):
    if group in config["assignments"]:
        return config["assignments"][group]
    digest = hashlib.sha256(dump([config["seed"], group]).encode()).digest()
    value = int.from_bytes(digest[:8], "big") / 2**64
    cumulative = 0
    for name in ("train", "validation", "test"):
        cumulative += config["fractions"][name]
        if value < cumulative:
            return name
    return "test"


def ingest(db, paths, metadata, counts):
    for path in paths:
        for envelope in jsonl(path):
            if "event" in envelope and envelope["event"] not in TELEMETRY_EVENTS:
                counts["non_telemetry_events"] += 1
                continue
            batch = envelope.get("properties", envelope)
            if not isinstance(batch, dict) or batch.get("schema_version") not in SCHEMAS:
                raise ValueError("Unsupported telemetry schema")
            recorder = identifier(batch.get("recorder_id"), "recorder_id")
            if recorder not in metadata:
                raise ValueError(f"Missing recording metadata for {recorder}")
            seq = batch.get("batch_sequence")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0 or not finite(batch.get("time_origin_ms")):
                raise ValueError("Invalid batch sequence/time origin")
            records = decode_records(batch)
            if not isinstance(batch.get("capture", {}), dict):
                raise ValueError("Invalid capture metadata")
            quality = batch.get("quality")
            if quality is not None and (not isinstance(quality, dict) or not isinstance(quality.get("dropped_before_batch"), int) or isinstance(quality.get("dropped_before_batch"), bool) or quality["dropped_before_batch"] < 0):
                raise ValueError("Invalid quality metadata")
            # Ignore SDK enrichment (URLs, identities, timestamps) when deduplicating source data.
            normalized = {"records": records, "time_origin_ms": batch["time_origin_ms"], "clock": batch.get("clock"),
                          "quality": batch.get("quality"), "capture": batch.get("capture", {})}
            payload = dump(normalized)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            previous = db.execute("SELECT digest FROM batches WHERE recorder=? AND seq=?", (recorder, seq)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ValueError(f"Conflicting duplicate batch: {recorder}/{seq}")
                counts["duplicate_batches"] += 1
                continue
            db.execute("INSERT INTO batches VALUES (?,?,?,?)", (recorder, seq, digest, payload))
            counts["batches"] += 1
    db.commit()


def normalize(db, counts):
    for (recorder,) in db.execute("SELECT DISTINCT recorder FROM batches ORDER BY recorder"):
        previous_seq = previous_end = previous_origin = previous_offset = None
        previous_dropped = 0
        held, next_press, ordinal = {}, 0, 0
        for seq, payload in db.execute("SELECT seq,payload FROM batches WHERE recorder=? ORDER BY seq", (recorder,)):
            b = json.loads(payload)
            start = min(r["t"] for r in b["records"])
            end = max(r["t"] for r in b["records"])
            reasons = []
            if previous_seq is not None and seq != previous_seq + 1:
                counts["missing_batches"] += seq - previous_seq - 1
                reasons.append("missing-batch")
            if previous_origin is not None and previous_origin != b["time_origin_ms"]:
                raise ValueError("One recorder_id cannot have multiple time origins")
            quality = b.get("quality")
            known = isinstance(quality, dict) and isinstance(quality.get("dropped_before_batch"), int)
            if known:
                dropped = quality["dropped_before_batch"]
                if dropped < previous_dropped:
                    raise ValueError("Cumulative dropped count went backwards")
                if dropped > previous_dropped:
                    reasons.append("reported-loss")
                previous_dropped = dropped
            clock = b.get("clock")
            if clock is not None:
                if not isinstance(clock, dict) or not finite(clock.get("epoch_ms")) or not finite(clock.get("perf_ms")):
                    raise ValueError("Invalid clock anchor")
                offset = clock["epoch_ms"] - clock["perf_ms"]
                if previous_offset is not None and abs(offset - previous_offset) > 100:
                    reasons.append("clock-discontinuity")
                previous_offset = offset
            if reasons:
                held.clear()
                for reason in reasons:
                    db.execute("INSERT INTO gaps VALUES (?,?,?,?)", (recorder, min(previous_end if previous_end is not None else start, start), start, reason))
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (recorder, start, ordinal, dump({"kind": "boundary", "t": start, "reason": reasons[0]}), 0, int(known)))
                ordinal += 1
            for row in b["records"]:
                kind, t = row["kind"], row["t"]
                clean = {"kind": kind, "t": t}
                if "trusted" in row:
                    clean["trusted"] = row["trusted"]
                if kind == "boundary":
                    held.clear()
                    reason = row.get("reason", "boundary")
                    if not isinstance(reason, str):
                        raise ValueError("Invalid boundary reason")
                    clean["reason"] = reason
                    lo = min(previous_end if previous_end is not None else t, t)
                    db.execute("INSERT INTO gaps VALUES (?,?,?,?)", (recorder, lo, t, reason))
                if kind in ("keydown", "keyup"):
                    # IDs identify a press, never a physical key across multiple presses.
                    source = ("press", row["press_id"]) if "press_id" in row else ("code", row.get("code"), row.get("location", 0))
                    pairable = "press_id" in row or bool(row.get("code"))
                    if kind == "keydown" and not row.get("repeat", False) and pairable:
                        next_press += 1; held[source] = next_press
                    if pairable and source in held:
                        clean["press_id"] = held[source]
                    if kind == "keyup":
                        held.pop(source, None)
                    clean["repeat"] = row.get("repeat", False)
                if kind in ("pointerdown", "pointerup", "pointercancel"):
                    pointer = row.get("pointer_id", 0)
                    button = row.get("button", 0)
                    source = ("pointer", pointer, button)
                    if kind == "pointerdown":
                        next_press += 1; held[source] = next_press
                    if source in held:
                        clean["press_id"] = held[source]
                    if kind == "pointerup":
                        held.pop(source, None)
                    if kind == "pointercancel":
                        held = {k:v for k,v in held.items() if not (k[0] == "pointer" and k[1] == pointer)}
                for key in NUMERIC_FIELDS - {"button"}:
                    if key in row:
                        clean[key] = row[key]
                synthetic = row.get("trusted") is False or b["capture"].get("synthetic_allowed") is True
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (recorder, t, ordinal, dump(clean), int(synthetic), int(known)))
                ordinal += 1
                previous_end = max(previous_end if previous_end is not None else t, t)
            previous_seq, previous_origin = seq, b["time_origin_ms"]
            previous_end = max(previous_end, end)
        counts["records"] += ordinal
    db.commit()


def read_labels(path, metadata, recipe):
    seen, intervals = set(), {}
    labels = []
    for label in jsonl(path):
        label_id = identifier(label.get("id"), "label id")
        if label_id in seen:
            raise ValueError(f"Duplicate label id {label_id}")
        seen.add(label_id)
        recorder = identifier(label.get("recorder_id"), "recorder_id")
        if recorder not in metadata:
            raise ValueError("Label references unknown recorder_id")
        if label.get("timebase") != "recorder_ms":
            raise ValueError("Labels must explicitly use timebase=recorder_ms")
        a, b = label.get("start_ms"), label.get("end_ms")
        if not finite(a) or not finite(b) or a >= b:
            raise ValueError("Label requires finite start_ms < end_ms")
        source = label.get("source")
        if not isinstance(source, str) or source not in {"human", "self_report", "model", "heuristic", "synthetic"}:
            raise ValueError("Label requires a known source")
        identifier(label.get("version"), "label version")
        if not finite(label.get("confidence")) or not 0 <= label["confidence"] <= 1:
            raise ValueError("Label requires confidence in [0,1]")
        values = label.get("targets", {})
        if not isinstance(values, dict):
            raise ValueError("Label targets must be an object")
        for name, target in recipe["targets"].items():
            if name not in values:
                raise ValueError(f"Label {label_id} is missing target {name}")
            value = values[name]
            if target["type"] == "categorical" and value not in target["classes"]:
                raise ValueError(f"Unknown class for {name}")
            if target["type"] == "multilabel" and (not isinstance(value, list) or any(v not in target["classes"] for v in value) or len(set(value)) != len(value)):
                raise ValueError(f"Invalid multilabel target {name}")
            if target["type"] == "continuous" and (not finite(value) or not target["min"] <= value <= target["max"]):
                raise ValueError(f"Invalid continuous target {name}")
        if source in recipe["labels"]["sources"] and label["confidence"] >= recipe["labels"]["min_confidence"]:
            intervals.setdefault(recorder, []).append((a, b))
        labels.append(label)
    for spans in intervals.values():
        end = -math.inf
        for a, b in sorted(spans):
            if a < end:
                raise ValueError("Overlapping label intervals must be adjudicated before building")
            end = b
    return sorted(labels, key=lambda x: (x["recorder_id"], x["start_ms"], x["id"]))


def windows(label, config):
    a, b = label["start_ms"], label["end_ms"]
    if config["mode"] == "label":
        yield a, b
    else:
        if b - a < config["size_ms"]:
            yield a, b  # A rejected candidate, so short annotations are visible in the audit.
            return
        count = max(0, math.floor((b - a - config["size_ms"]) / config["stride_ms"]) + 1)
        if count > config["max_windows_per_label"]:
            raise ValueError("Annotation exceeds max_windows_per_label; shorten it or explicitly raise the recipe limit")
        while a + config["size_ms"] <= b:
            yield a, a + config["size_ms"]
            a += config["stride_ms"]


def load_hook(recipe, recipe_dir):
    path = recipe["features"]["hook"]
    if path is None:
        return None, None
    path = (recipe_dir / path).resolve()
    spec = importlib.util.spec_from_file_location("telemetry_recipe_hook", path)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load feature hook")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "extract", None)):
        raise ValueError("Feature hook must define extract(events, context)")
    return module.extract, fingerprint(path)


def build_dataset(recipe_path, batch_paths, labels_path, recordings_path, output):
    recipe_path, output = Path(recipe_path), Path(output)
    recipe = recipe_config(json.loads(recipe_path.read_text()))
    if not batch_paths:
        raise ValueError("At least one input JSONL file is required")
    if output.exists():
        raise ValueError("Output already exists; choose a new directory to preserve prior datasets")
    metadata = {}
    for row in jsonl(recordings_path):
        recorder = identifier(row.get("recorder_id"), "recorder_id")
        identifier(row.get("group_id"), "group_id")
        if recorder in metadata:
            raise ValueError("Duplicate recording metadata")
        metadata[recorder] = row
    # One person/session must not accidentally be assigned to multiple declared groups.
    for field in ("subject_id", "session_id"):
        owners = {}
        for row in metadata.values():
            if field not in row:
                continue
            value = identifier(row[field], field)
            if value in owners and owners[value] != row["group_id"] and not (field == "subject_id" and recipe["split"]["allow_subject_overlap"]):
                raise ValueError(f"{field} crosses groups; within-person protocols must explicitly set split.allow_subject_overlap")
            owners[value] = row["group_id"]
    assignments = recipe["split"]["assignments"]
    if set(assignments) - {r["group_id"] for r in metadata.values()}:
        raise ValueError("Split assignment references an unknown group")
    labels = read_labels(labels_path, metadata, recipe)
    hook, hook_hash = load_hook(recipe, recipe_path.parent)
    counts, rejects, target_counts = Counter(), Counter(), Counter()
    groups = {r["group_id"]: split_for(r["group_id"], recipe["split"]) for r in metadata.values()}
    provenance = {"batches": sorted(fingerprint(p) for p in batch_paths), "labels": fingerprint(labels_path),
                  "recordings": fingerprint(recordings_path), "recipe": hashlib.sha256(dump(recipe).encode()).hexdigest(), "feature_hook": hook_hash}
    provenance["builder_files"] = {p.name: fingerprint(p) for p in sorted(Path(__file__).parent.glob("*.py"))}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a completed dataset; the SQLite spool and failed builds stay temporary.
    with tempfile.TemporaryDirectory(prefix="telemetry-build-", dir=output.parent) as temporary:
        root = Path(temporary)
        with closing(sqlite3.connect(root / "staging.sqlite")) as db:
            db.executescript("""
                CREATE TABLE batches(recorder TEXT, seq INTEGER, digest TEXT, payload TEXT, PRIMARY KEY(recorder,seq));
                CREATE TABLE events(recorder TEXT, t REAL, ordinal INTEGER, payload TEXT, synthetic INTEGER, quality_known INTEGER);
                CREATE TABLE gaps(recorder TEXT, start REAL, end REAL, reason TEXT);
            """)
            ingest(db, batch_paths, metadata, counts)
            normalize(db, counts)
            db.executescript("CREATE INDEX event_window ON events(recorder,t,ordinal); CREATE INDEX gap_window ON gaps(recorder,start,end);")
            result_dir = root / "dataset"; result_dir.mkdir()
            files = {name: (result_dir / f"{name}.jsonl").open("w") for name in ("train", "validation", "test", "rejected")}
            feature_names = None
            try:
                for label in labels:
                    recorder = label["recorder_id"]
                    for a, b in windows(label, recipe["window"]):
                        example_id = hashlib.sha256(dump([recorder, a, b, label["id"]]).encode()).hexdigest()[:24]
                        reason = None
                        if recipe["window"]["mode"] == "sliding" and b - a < recipe["window"]["size_ms"]:
                            reason = "annotation-too-short"
                        elif label["source"] not in recipe["labels"]["sources"] or label["confidence"] < recipe["labels"]["min_confidence"]:
                            reason = "label-policy"
                        elif b - a > recipe["window"]["max_duration_ms"]:
                            reason = "window-too-long"
                        if reason is None:
                            rows = db.execute("SELECT payload,synthetic,quality_known FROM events WHERE recorder=? AND t>=? AND t<? ORDER BY t,ordinal LIMIT ?",
                                              (recorder, a, b, recipe["window"]["max_events"] + 1)).fetchall()
                            events = [json.loads(r[0]) for r in rows]
                            gaps = [r[0] for r in db.execute("SELECT DISTINCT reason FROM gaps WHERE recorder=? AND start<? AND end>=? ORDER BY reason", (recorder, b, a))]
                            if len(rows) > recipe["window"]["max_events"]: reason = "too-many-events"
                            elif sum(r["kind"] != "boundary" for r in events) < recipe["quality"]["min_events"]: reason = "too-few-events"
                            elif gaps and not recipe["quality"]["allow_gaps"]: reason = "capture-gap"
                            elif any(r[1] for r in rows) and not recipe["quality"]["allow_synthetic"]: reason = "synthetic-input"
                            elif not all(r[2] for r in rows) and recipe["quality"]["require_quality_metadata"]: reason = "unknown-quality"
                        if reason:
                            rejects[reason] += 1
                            files["rejected"].write(dump({"id": example_id, "label_id": label["id"], "reason": reason, "start_ms": a, "end_ms": b}) + "\n")
                            continue
                        context = {"start_ms": a, "end_ms": b}
                        features, quality = extract_features(events, context, recipe["features"]["families"])
                        if hook:
                            # Hooks receive sanitized telemetry + window bounds, never labels or group IDs.
                            extra = hook(json.loads(dump(events)), dict(context))
                            if not isinstance(extra, dict) or set(extra) & set(features):
                                raise ValueError("Feature hook must return an object with unique feature names")
                            features.update(extra)
                        if any(not isinstance(k, str) or not k or (v is not None and not finite(v)) for k, v in features.items()):
                            raise ValueError("Features must be named finite numbers or null")
                        if set(recipe["features"]["exclude"]) - set(features):
                            raise ValueError("features.exclude names an unknown feature")
                        for key in recipe["features"]["exclude"]:
                            features.pop(key, None)
                        names = sorted(features)
                        if feature_names is not None and names != feature_names:
                            raise ValueError("Feature schema changed between windows")
                        feature_names = names
                        group = metadata[recorder]["group_id"]; split = groups[group]
                        quality.update(event_count=sum(r["kind"] != "boundary" for r in events), gap_reasons=gaps,
                                       first_event_ms=events[0]["t"], last_event_ms=events[-1]["t"],
                                       unknown_trust_events=sum("trusted" not in r for r in events if r["kind"] != "boundary"),
                                       quality_metadata_known=all(r[2] for r in rows), synthetic=any(r[1] for r in rows))
                        example = {"id": example_id, "recorder_id": recorder, "group_id": group, "split": split,
                                   "start_ms": a, "end_ms": b, "features": features, "quality": quality,
                                   "targets": {k: label["targets"][k] for k in recipe["targets"]},
                                   "label": {k: label[k] for k in ("id", "source", "version", "confidence")}}
                        if recipe["output"]["include_sequence"]:
                            example["sequence"] = [{**r, "t": r["t"] - a} for r in events]
                        files[split].write(dump(example) + "\n")
                        counts[f"{split}_examples"] += 1
                        for name, target in recipe["targets"].items():
                            value = example["targets"][name]
                            for category in (value if target["type"] == "multilabel" else [value] if target["type"] == "categorical" else ["numeric"]):
                                target_counts[f"{split}/{name}/{category}"] += 1
            finally:
                for f in files.values(): f.close()
            warnings = [f"No accepted examples in {s}" for s in ("train", "validation", "test") if not counts[f"{s}_examples"]]
            if any(l["source"] in ("model", "heuristic") for l in labels):
                warnings.append("Proxy labels are not direct measurements of the user's emotional state")
            if recipe["split"]["allow_subject_overlap"]:
                warnings.append("Within-person protocol: subject overlap is allowed; this cannot establish generalization to new people")
            for split in ("train", "validation", "test"):
                for name, target in recipe["targets"].items():
                    for category in target.get("classes", []):
                        if not target_counts[f"{split}/{name}/{category}"]:
                            warnings.append(f"No examples of {name}={category} in {split}")
            manifest = {"format_version": 1, "builder_version": __version__, "recipe": recipe, "provenance": provenance,
                        "features": feature_names or [], "groups": groups, "counts": dict(sorted(counts.items())),
                        "rejections": dict(sorted(rejects.items())), "target_counts": dict(sorted(target_counts.items())), "warnings": warnings,
                        "output_sha256": {p.name: fingerprint(p) for p in sorted(result_dir.glob("*.jsonl"))}}
            (result_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        result_dir.rename(output)
    return manifest
