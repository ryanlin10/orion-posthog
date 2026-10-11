"""`orion deploy`: put a trained model where the site serves static files, for orion.js to label live sessions."""
import json
from datetime import datetime, timezone
from pathlib import Path

LIVE_KINDS = {"linear", "forest", "boosted", "onnx"}


def deploy(workspace, name, out_dir, version=None, context=None, url_prefix="/orion/"):
    model_dir = workspace.path("models", name, f"v{version}") if version else workspace.latest("models", name)
    if not model_dir.exists():
        raise FileNotFoundError(f"No model {name} v{version}")
    spec = json.loads((model_dir / "model.json").read_text())
    if spec["estimator"]["kind"] not in LIVE_KINDS:
        raise ValueError(f"{spec['estimator']['kind']} models cannot run in the browser")
    recipe = json.loads((model_dir / "recipe.json").read_text())
    if recipe["features"].get("hook"):
        raise ValueError("This model uses a Python feature hook, which the browser cannot run")
    if context is not None:
        spec["agents"] = list(context)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}.json"
    target.write_text(json.dumps(spec, separators=(",", ":")))
    extra = []
    if spec["estimator"]["kind"] == "onnx":
        onnx = model_dir / spec["estimator"]["file"]
        (out_dir / onnx.name).write_bytes(onnx.read_bytes())
        extra.append(out_dir / onnx.name)
    url = f"{url_prefix.rstrip('/')}/{target.name}"
    record = {"version": spec["version"], "file": str(target.resolve()), "url": url, "agents": spec.get("agents", []),
              "deployed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    workspace.path("models", name, "deployed.json").write_text(json.dumps(record, indent=2) + "\n")
    snippet = (f"<script>window.orionOptions = {{ mask: 'timing', models: ['{url}'] }};</script>\n"
               f"<script src=\"/vendor/orion.js\" defer></script>  <!-- dist/orion.js from `npm run build` -->\n\n"
               f"// or, with a bundler:\n"
               f"orion.connect(posthog, {{ mask: 'timing', models: ['{url}'] }})")
    return target, extra, snippet, spec
