"""Sequence classifiers (GRU, temporal CNN, transformer) on time-step encodings of each window.

The window is cut into fixed steps (100 ms by default) with 14 channels each, mirroring src/sequence.js.
Models are trained with PyTorch (pip install 'paradigm-orion[sequence]'), checked on held-out groups like the
tabular models, and exported to ONNX, which orion.js runs in the browser through onnxruntime-web.
"""
import hashlib
import json
import math
import os
import shutil
from datetime import datetime, timezone

import numpy as np

CHANNELS = ["moves", "path", "peak_speed", "turning", "pointer_downs", "pointer_ups", "click_hold", "key_downs", "key_ups",
            "key_hold", "wheel", "focus_changes", "gap", "active"]
# The telemetry family behind each channel; capture gaps and activity belong to every recipe.
CHANNEL_FAMILY = {"moves": "pointer", "path": "pointer", "peak_speed": "pointer", "turning": "pointer", "pointer_downs": "pointer",
                  "pointer_ups": "pointer", "click_hold": "pointer", "key_downs": "keyboard", "key_ups": "keyboard", "key_hold": "keyboard",
                  "wheel": "scroll", "focus_changes": "focus", "gap": None, "active": None}
KIND_FAMILY = {"pointermove": "pointer", "pointerdown": "pointer", "pointerup": "pointer", "pointercancel": "pointer", "click": "pointer",
               "dblclick": "pointer", "contextmenu": "pointer", "keydown": "keyboard", "keyup": "keyboard", "wheel": "scroll",
               "scroll": "scroll", "focusin": "focus", "focusout": "focus"}


def channels_for(families):
    """The channels a recipe's telemetry families keep."""
    return [c for c in CHANNELS if CHANNEL_FAMILY[c] is None or CHANNEL_FAMILY[c] in families]


def encode_steps(events, duration_ms, steps, step_ms, channels=CHANNELS):
    """events: normalized rows sorted by t, t relative to the window start. Returns float32 [steps, len(channels)].
    Input from families without a channel is skipped entirely, so it does not show up in `active` either."""
    C = len(CHANNELS)
    out = np.zeros((steps, C), dtype=np.float32)
    path, peak, turn = np.zeros(steps), np.zeros(steps), np.zeros(steps)
    click_hold, key_hold = np.zeros(steps), np.zeros(steps)
    offset = steps * step_ms - duration_ms
    pointers, keys, clicks = {}, {}, {}
    kept = {CHANNEL_FAMILY[c] for c in channels}
    for row in events:
        i = math.floor((row["t"] + offset) / step_ms)
        family = KIND_FAMILY.get(row["kind"])
        if i < 0 or i >= steps or (family and family not in kept):
            continue
        out[i, 13] = 1
        kind = row["kind"]
        if kind == "boundary":
            out[i, 12] = 1
            pointers, keys, clicks = {}, {}, {}
        elif kind == "pointermove":
            out[i, 0] += 1
            pid = row.get("pointer_id", 0)
            prev = pointers.get(pid)
            if prev and row["t"] > prev["t"] and row["t"] - prev["t"] < 100 and "x" in row and "y" in row:
                dx, dy = row["x"] - prev["x"], row["y"] - prev["y"]
                d = math.hypot(dx, dy)
                path[i] += d
                peak[i] = max(peak[i], d * 1000 / (row["t"] - prev["t"]))
                if d > 0.5 and prev["d"] > 0.5:
                    cos = (dx * prev["dx"] + dy * prev["dy"]) / (d * prev["d"])
                    turn[i] += math.acos(max(-1.0, min(1.0, cos)))
                pointers[pid] = {"t": row["t"], "x": row["x"], "y": row["y"], "dx": dx, "dy": dy, "d": d}
            elif "x" in row and "y" in row:
                pointers[pid] = {"t": row["t"], "x": row["x"], "y": row["y"], "dx": 0, "dy": 0, "d": 0}
        elif kind == "pointerdown":
            out[i, 4] += 1
            if "press_id" in row:
                clicks[row["press_id"]] = row["t"]
        elif kind == "pointerup":
            out[i, 5] += 1
            down = clicks.pop(row.get("press_id"), None)
            if down is not None and row["t"] >= down:
                click_hold[i] += row["t"] - down
        elif kind == "keydown":
            if not row.get("repeat", False):
                out[i, 7] += 1
                if "press_id" in row:
                    keys[row["press_id"]] = row["t"]
        elif kind == "keyup":
            out[i, 8] += 1
            down = keys.pop(row.get("press_id"), None)
            if down is not None and row["t"] >= down:
                key_hold[i] += row["t"] - down
        elif kind == "wheel":
            out[i, 10] += 1
        elif kind in ("focusin", "focusout"):
            out[i, 11] += 1
    out[:, 1], out[:, 2], out[:, 3] = np.log1p(path), np.log1p(peak), np.log1p(turn)
    out[:, 6], out[:, 9] = np.log1p(click_hold), np.log1p(key_hold)
    return out if channels == CHANNELS else out[:, [CHANNELS.index(c) for c in channels]]


def _torch():
    try:
        import torch
        from torch import nn
    except ImportError:  # pragma: no cover - depends on the optional extra
        raise RuntimeError("Sequence models need PyTorch: pip install 'paradigm-orion[sequence]'") from None
    return torch, nn


def build_network(architecture, steps, channels, outputs, params, mean, std):
    torch, nn = _torch()
    hidden = int(params.get("hidden", 64))

    class Standardise(nn.Module):
        def __init__(self):
            super().__init__()
            self.register_buffer("mean", torch.tensor(mean, dtype=torch.float32))
            self.register_buffer("std", torch.tensor(std, dtype=torch.float32))

        def forward(self, x):
            return (x - self.mean) / self.std

    class GRUNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.norm, self.proj = Standardise(), nn.Linear(channels, hidden)
            self.gru = nn.GRU(hidden, hidden, num_layers=int(params.get("layers", 2)), batch_first=True)
            self.head = nn.Linear(hidden, outputs)

        def forward(self, x):
            h, _ = self.gru(torch.relu(self.proj(self.norm(x))))
            return self.head(h[:, -1])

    class TCN(nn.Module):
        def __init__(self):
            super().__init__()
            self.norm, self.proj = Standardise(), nn.Conv1d(channels, hidden, 1)
            kernel = int(params.get("kernel", 5))
            self.blocks = nn.ModuleList()
            self.pads = []
            for layer in range(int(params.get("layers", 6))):
                dilation = 2 ** layer
                self.pads.append((kernel - 1) * dilation)
                self.blocks.append(nn.Conv1d(hidden, hidden, kernel, dilation=dilation))
            self.head = nn.Linear(hidden, outputs)

        def forward(self, x):
            h = self.proj(self.norm(x).transpose(1, 2))
            for pad, conv in zip(self.pads, self.blocks):
                h = h + torch.relu(conv(nn.functional.pad(h, (pad, 0))))  # Causal: each step sees only the past.
            return self.head(h.mean(dim=2))

    class Transformer(nn.Module):
        def __init__(self):
            super().__init__()
            heads = int(params.get("heads", 4))
            self.norm, self.proj = Standardise(), nn.Linear(channels, hidden)
            self.position = nn.Parameter(torch.zeros(1, steps, hidden))
            layer = nn.TransformerEncoderLayer(hidden, heads, dim_feedforward=hidden * 2, dropout=0.1, batch_first=True)
            self.encoder = nn.TransformerEncoder(layer, num_layers=int(params.get("layers", 4)), enable_nested_tensor=False)
            self.head = nn.Linear(hidden, outputs)

        def forward(self, x):
            h = self.encoder(self.proj(self.norm(x)) + self.position)
            return self.head(h.mean(dim=1))

    return {"gru": GRUNet, "tcn": TCN, "transformer": Transformer}[architecture]()


class TorchClassifier:
    """predict / predict_proba / classes_ like scikit-learn, so evaluation code is shared."""

    def __init__(self, net, classes, continuous):
        self.net, self.classes_, self.continuous = net, np.array(classes) if classes else None, continuous

    def _out(self, x):
        torch, _ = _torch()
        self.net.eval()
        with torch.no_grad():
            return self.net(torch.as_tensor(x, dtype=torch.float32)).numpy()

    def predict_proba(self, x):
        out = self._out(x)
        if out.shape[1] == 1:
            p = 1 / (1 + np.exp(-out[:, 0]))
            return np.stack([1 - p, p], axis=1)
        e = np.exp(out - out.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True)

    def predict(self, x):
        if self.continuous:
            return self._out(x)[:, 0]
        return self.classes_[self.predict_proba(x).argmax(axis=1)]


def fit(architecture, x, y, x_val, y_val, target, params, seed, log=lambda *a: None, curve=None):
    """curve: optional (x, y) scored after every epoch for the report's loss curve; it never steers training."""
    torch, nn = _torch()
    torch.set_num_threads(os.cpu_count() or 1)
    torch.manual_seed(seed)
    np.random.seed(seed % (2 ** 32))
    continuous = target["type"] == "continuous"
    classes = None if continuous else sorted(set(y))
    flat = x.reshape(-1, x.shape[2])
    mean, std = flat.mean(axis=0), flat.std(axis=0)
    std[std < 1e-6] = 1.0
    outputs = 1 if continuous or len(classes) == 2 else len(classes)
    net = build_network(architecture, x.shape[1], x.shape[2], outputs, params, mean, std)
    if continuous:
        targets, loss_fn = torch.tensor(np.asarray(y, dtype=np.float32)).unsqueeze(1), nn.MSELoss()
    else:
        index = {c: i for i, c in enumerate(classes)}
        codes = np.array([index[v] for v in y])
        counts = np.bincount(codes, minlength=len(classes))
        weights = len(codes) / (len(classes) * np.maximum(counts, 1))
        if outputs == 1:
            targets = torch.tensor(codes, dtype=torch.float32).unsqueeze(1)
            loss_fn = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([weights[1] / weights[0]], dtype=torch.float32))
        else:
            targets, loss_fn = torch.tensor(codes), nn.CrossEntropyLoss(weight=torch.tensor(weights, dtype=torch.float32))
    inputs = torch.tensor(x)
    optimiser = torch.optim.AdamW(net.parameters(), lr=float(params.get("learning_rate", 1e-3)), weight_decay=1e-4)
    epochs, patience, batch = int(params.get("epochs", 40)), int(params.get("patience", 6)), int(params.get("batch", 64))
    best, best_state, waited = math.inf, None, 0
    def tensors(xs, ys):
        """Inputs and targets for scoring, without examples whose class the model never saw."""
        if not len(xs):
            return None, None
        if continuous:
            return torch.tensor(xs), torch.tensor(np.asarray(ys, dtype=np.float32)).unsqueeze(1)
        keep = [i for i, v in enumerate(ys) if v in index]
        codes = torch.tensor([index[ys[i]] for i in keep])
        return torch.tensor(xs)[keep], codes.float().unsqueeze(1) if outputs == 1 else codes

    val_inputs, val_targets = tensors(x_val, y_val)
    curve_inputs, curve_targets = tensors(*curve) if curve is not None else (None, None)
    history = []
    generator = torch.Generator().manual_seed(seed)
    for epoch in range(epochs):
        net.train()
        order = torch.randperm(len(inputs), generator=generator)
        total = 0.0
        for start in range(0, len(order), batch):
            idx = order[start:start + batch]
            optimiser.zero_grad()
            loss = loss_fn(net(inputs[idx]), targets[idx])
            loss.backward()
            optimiser.step()
            total += loss.item() * len(idx)
        net.eval()
        with torch.no_grad():
            monitored = float(loss_fn(net(val_inputs), val_targets)) if val_inputs is not None and len(val_inputs) else float(loss_fn(net(inputs), targets))
            held_out = float(loss_fn(net(curve_inputs), curve_targets)) if curve_inputs is not None and len(curve_inputs) else None
        history.append({"epoch": epoch + 1, "train": round(total / len(order), 5), "early_stopping": round(monitored, 5),
                        "validation": None if held_out is None else round(held_out, 5)})
        if monitored < best - 1e-4:
            best, waited = monitored, 0
            best_state = {k: v.clone() for k, v in net.state_dict().items()}
        else:
            waited += 1
            if waited >= patience:
                break
    if best_state:
        net.load_state_dict(best_state)
    log(f"  stopped after {epoch + 1} epochs (best monitored loss {best:.4f})")
    model = TorchClassifier(net, classes, continuous)
    model.history = history
    return model


def export_onnx(model, path, steps, channels):
    torch, _ = _torch()
    model.net.eval()
    dummy = torch.zeros(1, steps, channels)
    torch.onnx.export(model.net, (dummy,), str(path), input_names=["steps"], output_names=["out"],
                      dynamic_axes={"steps": {0: "batch"}, "out": {0: "batch"}}, opset_version=17, dynamo=False)


def check_onnx(model, path, sample):
    import onnxruntime
    session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    got = session.run(None, {"steps": sample.astype(np.float32)})[0]
    expected = model._out(sample)
    np.testing.assert_allclose(got, expected, rtol=1e-3, atol=1e-4)


def train_sequence(workspace, recipe, dataset_dir, evaluate_test, log, shuffles=3):
    from .export import FORMAT, export_model
    from .train import SPLITS, load_dataset, matrix, metrics, plain_summary, score
    dataset_dir = dataset_dir or workspace.latest("datasets", recipe["name"])
    manifest, rows = load_dataset(dataset_dir)
    if not manifest["recipe"]["output"]["include_sequence"]:
        raise ValueError("This dataset was built without sequences; rebuild it with a sequence architecture in the recipe")
    target, architecture, params = recipe["target"], recipe["model"]["architecture"], recipe["model"]["params"]
    step_ms = int(params.get("step_ms", 100))
    seed = int(hashlib.sha256(recipe["name"].encode()).hexdigest()[:8], 16)
    steps = math.ceil(recipe["window"]["length_ms"] / step_ms)
    channels = channels_for(recipe["telemetry"])
    x = {s: np.stack([encode_steps(r["sequence"], r["end_ms"] - r["start_ms"], steps, step_ms, channels) for r in rows[s]]) if rows[s]
         else np.zeros((0, steps, len(channels)), np.float32) for s in SPLITS}
    y = {s: [r["targets"][target["name"]] for r in rows[s]] for s in SPLITS}
    if not rows["train"]:
        raise ValueError("No training examples; label more windows first")
    # Early stopping watches a slice of the training groups, so the validation score stays untouched.
    groups = [r["group_id"] for r in rows["train"]]
    inner = np.array([int(hashlib.sha256(f"{seed}:{g}".encode()).hexdigest()[:8], 16) % 100 < 15 for g in groups])
    if inner.all() or not inner.any():
        inner = np.arange(len(groups)) % 7 == 0
    fit_x, stop_x = x["train"][~inner], x["train"][inner]
    fit_y = [v for v, m in zip(y["train"], inner) if not m]
    stop_y = [v for v, m in zip(y["train"], inner) if m]
    log(f"Training a {architecture} on {len(fit_y)} windows of {steps} steps × {len(channels)} channels ({len(stop_y)} more decide when to stop)")
    model = fit(architecture, fit_x, fit_y, stop_x, stop_y, target, params, seed, log, curve=(x["validation"], y["validation"]))
    names = manifest["features"]
    baseline = MostCommon(y["train"], target["type"] == "continuous")
    loss = "mean squared error" if target["type"] == "continuous" else "cross-entropy, weighted by class"
    report = {"recipe": recipe["name"], "architecture": architecture, "dataset": str(dataset_dir), "steps": steps, "step_ms": step_ms,
              "channels": channels, "counts": {s: len(rows[s]) for s in SPLITS}, "metrics": {}, "warnings": list(manifest.get("warnings", [])),
              "curve": {"loss": loss, "epochs": model.history,
                        "kept_epoch": min(model.history, key=lambda h: h["early_stopping"])["epoch"] if model.history else None}}
    for split in ("validation", "test") if evaluate_test else ("validation",):
        report["metrics"][split] = metrics(target, model, baseline, x[split], y[split])
    if rows["validation"] and shuffles:
        real = score(target, y["validation"], model, x["validation"])
        rng = np.random.default_rng(seed)
        others = []
        # Early stopping usually ends shuffled-label runs within a few epochs; the cap bounds the worst case.
        quick = {**params, "epochs": min(15, int(params.get("epochs", 40)))}
        for _ in range(shuffles):
            perm = list(rng.permutation(fit_y))
            if target["type"] != "continuous" and len(set(perm)) < 2:
                continue
            shuffled = fit(architecture, fit_x, perm, stop_x, stop_y, target, quick, int(rng.integers(1 << 30)))
            others.append(score(target, y["validation"], shuffled, x["validation"]))
        at_least = sum(s >= real for s in others)
        report["shuffled"] = {"rounds": len(others), "at_least_as_good": at_least, "p_value": (1 + at_least) / (1 + len(others)),
                              "shuffled_median": float(np.median(others)) if others else None}
    report["summary"] = plain_summary(target, report)
    version = workspace.next_version("models", recipe["name"])
    out = workspace.path("models", recipe["name"], f"v{version}")
    tmp = out.with_name(out.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    onnx_name = f"{recipe['name']}-v{version}.onnx"
    export_onnx(model, tmp / onnx_name, steps, len(channels))
    check_onnx(model, tmp / onnx_name, x["train"][:8])
    evidence = {}
    if names:
        # Window features still explain live labels in plain words, even though the network reads time steps.
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression, Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        helper = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True, add_indicator=True), StandardScaler(),
                               Ridge() if target["type"] == "continuous" else LogisticRegression(max_iter=1000))
        features = matrix(rows["train"], names)
        helper.fit(features, y["train"])
        evidence = export_model(helper, names, features, recipe, version, {})["evidence"]
    link = "identity" if target["type"] == "continuous" else ("logistic" if len(model.classes_) == 2 else "softmax")
    spec = {**FORMAT, "name": recipe["name"], "version": version,
            "target": {k: target[k] for k in ("name", "type", "classes", "min", "max", "binary") if k in target},
            "window": {"size_ms": recipe["window"]["length_ms"], "align": recipe["window"]["align"], "min_events": recipe["quality"]["min_events"],
                       "max_events": 100000, "allow_gaps": not recipe["quality"]["drop_gaps"]},
            "features": {"families": recipe["telemetry"], "exclude": recipe["features"]["exclude"], "names": names},
            "estimator": {"kind": "onnx", "file": onnx_name, "input": "steps", "output": "out", "steps": steps, "step_ms": step_ms,
                          "channels": channels, "link": link, "classes": [str(c) for c in model.classes_] if model.classes_ is not None else None},
            "evidence": evidence, "agents": recipe["deploy"]["context"],
            "live": {"every_ms": recipe["deploy"]["every_ms"], "heartbeat_ms": recipe["deploy"]["heartbeat_ms"], "confirm": recipe["deploy"]["confirm"]},
            "report": {"summary": report["summary"], "validation": report["metrics"].get("validation")},
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (tmp / "model.json").write_text(json.dumps(spec, separators=(",", ":"), allow_nan=False))
    (tmp / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    (tmp / "recipe.json").write_text(json.dumps(recipe, indent=2) + "\n")
    tmp.rename(out)
    log(f"Trained {recipe['name']} v{version} ({architecture}). {report['summary']}")
    return out, report


class MostCommon:
    """The training set's most common answer (or mean score) for every window: the floor a model must beat."""

    def __init__(self, y, continuous):
        if continuous:
            self.value = float(np.mean(y))
        else:
            values, counts = np.unique(np.asarray(y, dtype=object), return_counts=True)
            self.value = values[counts.argmax()]

    def predict(self, x):
        return np.full(len(x), self.value, dtype=object if isinstance(self.value, str) else float)
