"""Orion: fine-grained input telemetry on top of PostHog, sentiment classifiers trained from it, and live labels
for agents. Everything runs on your machine and in your visitors' browsers; nothing is hosted by Orion.

    from paradigm_orion import Orion
    orion = Orion(posthog_project="acme-web")
    clf = orion.train("recipes/checkout-sentiment.yaml")
    clf.deploy(out="public/orion", context=["support-agent"])
    orion.context("<session id>")
    orion.search(label="frustrated", page="/checkout", cohort="Paying customers")
"""
import json
from pathlib import Path

__version__ = "0.3.0"


class Classifier:
    def __init__(self, orion, path, report):
        self.orion, self.path, self.report = orion, Path(path), report
        self.spec = json.loads((self.path / "model.json").read_text())
        self.name, self.version = self.spec["name"], self.spec["version"]

    @property
    def summary(self):
        return self.report.get("summary", "")

    def deploy(self, out="public/orion", context=None, url_prefix="/orion/"):
        from .deploy import deploy
        target, _, snippet, _ = deploy(self.orion.workspace, self.name, out, self.version, context, url_prefix)
        return {"file": str(target), "snippet": snippet}

    def __repr__(self):
        return f"<Orion classifier {self.name} v{self.version}: {self.summary}>"


class Orion:
    def __init__(self, posthog_project=None, host=None, api_key=None, home=None, llm=None, log=print):
        from .posthog import PostHog
        from .store import Store
        from .workspace import Workspace
        self.workspace = Workspace(home)
        config = self.workspace.config()
        self.posthog = PostHog(host or config.get("posthog_host"), None, api_key, config.get("posthog_ui_host"))
        project = posthog_project or config.get("posthog_project")
        if project is not None and not str(project).isdigit():
            matches = [p for p in self.posthog.projects() if p.get("name") == project]
            if len(matches) != 1:
                raise ValueError(f"Found {len(matches)} PostHog projects named {project!r}; pass the numeric project id")
            project = matches[0]["id"]
        self.posthog.project_id = str(project or "")
        self.store = Store(self.workspace)
        self._llm, self.log = llm, log

    def llm(self, recipe):
        from .llm import make_client
        if "model" not in recipe["labels"]["by"]:
            return None
        return make_client(recipe["labels"]["model"]["llm"], self.workspace, self._llm)

    def recipe(self, recipe):
        from .recipe import load_recipe
        if isinstance(recipe, dict):
            return recipe
        path = Path(recipe)
        if path.exists():
            return load_recipe(path)
        for candidate in (Path("recipes") / f"{recipe}.yaml", Path("recipes") / f"{recipe}.yml", Path("recipes") / f"{recipe}.json"):
            if candidate.exists():
                return load_recipe(candidate)
        stored = self.workspace.latest("datasets", str(recipe)) / "orion.json"
        return json.loads(stored.read_text())["recipe"]

    def pull(self, since="30d", events=(), recipe=None):
        from .recipe import days
        text_events = ()
        if recipe is not None:
            r = self.recipe(recipe)
            if "text" in r["labels"]["model"]["reads"] and not r["labels"]["model"]["text"].get("file"):
                text_events = (r["labels"]["model"]["text"]["event"],)
        self.posthog.require_pull_access()  # Before any export exists: a missing scope stops here, with the fix.
        return self.store.pull(self.posthog, days(since, "since"), events, text_events, log=self.log)

    def build(self, recipe, relabel=False):
        from .build import build
        from .llm import LLMSetupError
        r = self.recipe(recipe)
        try:
            llm = self.llm(r)
        except LLMSetupError:
            llm = None  # Saved AI labels need no key; the build says so if it has to ask for new ones.
        return build(self.store, r, llm, relabel, self.log)

    def train(self, recipe, architecture=None, evaluate_test=False, rebuild=True, shuffles=None):
        from .train import train
        r = self.recipe(recipe)
        dataset = self.build(r)[0] if rebuild or not self.workspace.versions("datasets", r["name"]) else None
        path, report = train(self.workspace, r, dataset, architecture, evaluate_test, shuffles, self.log)
        return Classifier(self, path, report)

    def context(self, session_id, agent=None, text=False):
        from .agents import context, context_text
        ctx = context(self.posthog, session_id, agent)
        return context_text(ctx) if text else ctx

    def search(self, label=None, page=None, last="30d", model=None, limit=100, cohort=None):
        from .agents import parse_days, search
        return search(self.posthog, label, page, parse_days(last), model, limit, cohort)

    def survey(self, recipe, pages=20, out=None):
        """Rank the self-report answers for each page and mood (Jev when a TypeSafe key is saved, else the AI
        labeller's model) and save the survey file orion.js reads; with `out`, also copy it there."""
        from .credentials import get
        from .jev import Jev
        from .llm import make_client
        from .survey import build_survey, save_survey
        r = self.recipe(recipe)
        jev = Jev(self.workspace) if get("TYPESAFE_API_KEY") else None
        llm = None if jev else make_client(r["labels"]["model"]["llm"], self.workspace, self._llm)
        survey = build_survey(self.store, r, jev, llm, pages, self.log)
        path = save_survey(self.workspace, survey)
        if out is not None:
            target = Path(out) / f"{r['name']}-survey.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(survey, separators=(",", ":")))
            path = target
        return survey, path
