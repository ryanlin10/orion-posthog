"""The .orion/ folder in a project: pulled data, labels, datasets, versioned models and non-secret settings.
No secrets are stored here: keys live in the environment or in the keys file outside the project (credentials.py)."""
import json
import os
import re
from pathlib import Path

# llm_model is the AI labellers' model, e.g. "gpt-5.6-sol" or "openai:gpt-5.6-sol"; a recipe's own llm wins.
CONFIG_KEYS = {"posthog_host", "posthog_project", "posthog_ui_host", "llm_model"}


def find_root(start=None):
    """The nearest .orion/ in this folder or a folder above it (the way git finds .git), else this folder's.
    The home folder and the top of the disk never count: they hold many projects, not one."""
    here = Path(start or Path.cwd()).resolve()
    stops = {Path.home().resolve(), Path(here.anchor)}
    for folder in (here, *here.parents):
        if folder in stops:
            break
        if (folder / ".orion").is_dir():
            return folder / ".orion"
    return here / ".orion"


class Workspace:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("ORION_HOME") or find_root()).resolve()

    def misplaced(self):
        """Why this is not a project's folder, or None. Orion keeps one .orion/ per app, beside its code."""
        folder = self.root.parent
        if os.environ.get("ORION_HOME") or folder not in (Path.home().resolve(), Path(folder.anchor)):
            return None
        where = "your home folder" if folder == Path.home().resolve() else "the top of the disk"
        return (f"{folder} is {where}, not an app's folder. Go to your app's folder (cd my-app) and run this there: "
                "Orion keeps its data in .orion/ beside the app, and your keys and PostHog sign-in are shared by all of them.")

    def path(self, *parts):
        return self.root.joinpath(*parts)

    # Settings: environment variables win over .orion/config.json.
    def config(self):
        f = self.path("config.json")
        stored = json.loads(f.read_text()) if f.exists() else {}
        env = {"posthog_host": os.environ.get("POSTHOG_HOST"), "posthog_project": os.environ.get("POSTHOG_PROJECT_ID"),
               "posthog_ui_host": os.environ.get("POSTHOG_UI_HOST")}
        return {**stored, **{k: v for k, v in env.items() if v}}

    def save_config(self, **values):
        if set(values) - CONFIG_KEYS:
            raise ValueError(f"Unknown setting; use {', '.join(sorted(CONFIG_KEYS))}")
        self.root.mkdir(parents=True, exist_ok=True)
        f = self.path("config.json")
        stored = json.loads(f.read_text()) if f.exists() else {}
        stored.update({k: v for k, v in values.items() if v is not None})
        f.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")
        return stored

    def versions(self, kind, name):
        base = self.path(kind, name)
        if not base.exists():
            return []
        return sorted(int(m.group(1)) for p in base.iterdir() if (m := re.fullmatch(r"v(\d+)", p.name)) and p.is_dir())

    def next_version(self, kind, name):
        return max(self.versions(kind, name), default=0) + 1

    def latest(self, kind, name):
        existing = self.versions(kind, name)
        if not existing:
            raise FileNotFoundError(f"No {kind[:-1]} called {name} yet in {self.root}")
        return self.path(kind, name, f"v{existing[-1]}")


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    tmp.replace(path)


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]
