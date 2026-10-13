"""`orion check`: whether this folder, PostHog, the AI labeller and TypeSafe work, each with the step that fixes it.

Every check is live (a PostHog request, one tiny model request), so an agent can run it right after the person
finished `orion init` and know the setup works before anything else runs. Nothing printed holds a key.
"""
from . import credentials, posthog_auth
from .llm import KEY_NAMES, NAMES, LLMSetupError, default_model, split_model, verify, verify_typesafe
from .posthog import US_HOST, PostHog, PostHogError
from .workspace import Workspace

FIX = "run `orion init` (in a terminal it asks there; anywhere else it opens a setup page in the browser)"


def run_checks(ws=None):
    """[{"name", "ok", "detail", "fix"}]; ok is None for an optional part that is not set up."""
    ws = ws or Workspace()
    rows = []
    problem = ws.misplaced()
    rows.append({"name": "Folder", "ok": not problem, "detail": problem or str(ws.root.parent), "fix": None})
    rows.append(check_posthog(ws))
    rows.append(check_labeller(ws))
    key = credentials.get("TYPESAFE_API_KEY")
    if key:
        passed, message = verify_typesafe(key)
        rows.append({"name": "TypeSafe", "ok": passed, "detail": message, "fix": None if passed else FIX})
    else:
        rows.append({"name": "TypeSafe", "ok": None, "detail": "not set (optional: ranks the question's answers)", "fix": None})
    return rows


def check_posthog(ws):
    config = ws.config()
    host, project = config.get("posthog_host") or US_HOST, config.get("posthog_project")
    if not project:
        return {"name": "PostHog", "ok": False, "detail": "no PostHog project chosen for this folder", "fix": FIX}
    try:
        report = PostHog(host, project, None, config.get("posthog_ui_host")).access()
    except (PostHogError, posthog_auth.SignInError, OSError) as error:
        return {"name": "PostHog", "ok": False, "detail": str(error), "fix": FIX}
    via = "signed in" if report["via"] == "sign-in" else f"personal API key ({credentials.where('POSTHOG_PERSONAL_API_KEY')})"
    if report["missing"] or report["problem"]:
        why = report["problem"] or f"lacks {', '.join(report['missing'])}"
        return {"name": "PostHog", "ok": False, "detail": f"{via}; it {why}, so `orion pull` cannot run",
                "fix": "run `orion init` and sign in with PostHog: Orion then gets exactly the access it needs"}
    name = (report["project"] or {}).get("name") or project
    return {"name": "PostHog", "ok": True, "detail": f"{via}; project {name} ({project}) at {host}; can query and pull", "fix": None}


def check_labeller(ws):
    model = ws.config().get("llm_model")
    try:
        model = model or default_model()
        provider, name = split_model(model)
    except LLMSetupError:
        return {"name": "AI labeller", "ok": None, "fix": None,
                "detail": "not set up (optional: recipes can use self-report and human labels without it)"}
    key = credentials.get(KEY_NAMES[provider])
    if not key:
        return {"name": "AI labeller", "ok": False, "detail": f"{model} is chosen but there is no {NAMES[provider]} key", "fix": FIX}
    passed, message = verify(provider, key, name)
    return {"name": "AI labeller", "ok": passed, "detail": f"{model}: {message}", "fix": None if passed else FIX}


def print_checks(rows):
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        mark = {True: "✓", False: "✗", None: "-"}[r["ok"]]
        print(f"{mark} {r['name']:<{width}}  {r['detail']}")
        if r["fix"]:
            print(f"  {'':<{width}}  Fix: {r['fix']}")
