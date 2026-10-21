"""`orion init`, the setup step after installing (pip cannot ask questions during an install).

PostHog: signing in with the browser (posthog_auth.py) gives Orion exactly the scopes it needs, for the project the
person picks on PostHog's own page, so there is no key to paste and no scope to forget. A personal API key still
works (self-hosted PostHog, a computer without a browser): its scopes are checked before it is saved, and after
fixing them in PostHog the person checks again without pasting the key twice.

AI labeller and TypeSafe: each key is checked with one tiny real request, so a wrong key, an account without credit
or a model the key cannot use shows up here, with the page that fixes it.

In a terminal the questions are asked there. Without one (an agent's shell) `orion init` opens a setup page on
127.0.0.1 in the person's browser instead (setup_page.py), so keys never pass through the agent. Settings go to
.orion/config.json, keys and sign-ins to files outside the project. Keys are typed hidden and never printed.
"""
import getpass
import json
import os
import sys
import webbrowser
from pathlib import Path

from . import credentials, posthog_auth
from .llm import DEFAULT_MODELS, KEY_NAMES, KEY_PAGES, NAMES, PROVIDERS, LLMSetupError, provider_for, split_model, verify, verify_typesafe
from .posthog import US_HOST, PostHog, PostHogAccessError, PostHogError
from .workspace import Workspace

# The prompts and the browser; tests replace them.
ask = input
ask_secret = getpass.getpass
open_browser = webbrowser.open

POSTHOG_KEY, TYPESAFE_KEY = "POSTHOG_PERSONAL_API_KEY", "TYPESAFE_API_KEY"
POSTHOG_SCOPES = "project:read, query:read, batch_export:read and batch_export:write"


def init(args):
    ws = Workspace()
    problem = ws.misplaced()
    if problem:
        raise ValueError(problem)
    if args.no_input:
        return save_flags(ws, args)
    if args.terminal or (sys.stdin.isatty() and not args.browser):
        return in_terminal(ws, args)
    from .setup_page import run
    return run(ws, args)


def in_terminal(ws, args):
    print(f"Setting up Orion for {ws.root.parent}. Press Enter to accept the value in [brackets].")
    try:
        setup_posthog(ws, args)
        setup_labeller(ws, args)
        setup_typesafe(args)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Anything saved above is kept; run `orion init` again to finish.")
        return
    print("Setup done. `orion check` checks everything again at any time; next, `orion pull` exports your telemetry.")


def save_flags(ws, args):
    """No questions (--no-input): save the flags as given and say what is still missing."""
    model = model_from_flag(args.llm) if args.llm else None
    stored = ws.save_config(posthog_host=args.host, posthog_project=args.project, posthog_ui_host=args.ui_host, llm_model=model)
    print(f"Saved {ws.path('config.json')}: {json.dumps(stored)}")
    found = [name for name in credentials.NAMES if credentials.get(name)]
    print(f"Keys found: {', '.join(found) or 'none'}. Orion reads them from the environment or {credentials.keys_file()}.")
    host = stored.get("posthog_host") or US_HOST
    signed = posthog_auth.login(host, stored.get("posthog_project"))
    if signed:
        print(f"Signed in to PostHog at {host} for project {signed['project']}.")
    print("To sign in to PostHog and add keys, run `orion init` without --no-input: in a terminal it asks there, "
          "anywhere else it opens a setup page in the browser. `orion check` shows what works.")


def model_from_flag(value):
    """--llm takes a provider (meaning its default model) or a model name, which is saved as given."""
    value = value.strip()
    if value.lower() in PROVIDERS:
        return DEFAULT_MODELS[value.lower()]
    split_model(value)  # Fails now on a name no provider claims.
    return value


# PostHog

def setup_posthog(ws, args):
    saved = ws.config()
    host = full_url(args.host or ask_default("PostHog host", saved.get("posthog_host") or detect_host(ws.root.parent) or US_HOST))
    project = args.project or saved.get("posthog_project")
    current = posthog_auth.login(host, project)
    held = credentials.get(POSTHOG_KEY)
    if current:
        answer = ask(f"Signed in to PostHog for project {current['project']}. Keep it (Enter), sign in again (s), "
                     "or use a personal API key instead (k): ").strip().lower()
        if not answer.startswith(("s", "k")):
            save_settings(ws, posthog_host=host, posthog_project=current["project"], posthog_ui_host=args.ui_host)
            return report_access(host, current["project"])
        if answer.startswith("s"):
            return sign_in_here(ws, host, project, args.ui_host)
    elif held and not args.no_check:
        # A key saved earlier: say now whether it can do what `orion pull` needs, and offer the one-click fix if not.
        ok, message, missing, _ = check_posthog_key(host, held)
        print(f"PostHog personal API key ({credentials.where(POSTHOG_KEY)}): {message}")
        if ok:
            if not ask("Keep using it (Enter), or sign in with your browser instead (s): ").strip().lower().startswith("s"):
                save_settings(ws, posthog_host=host, posthog_project=project, posthog_ui_host=args.ui_host)
                return report_access(host, project) if project else posthog_key(ws, args, host, project, check_first=False)
            return sign_in_here(ws, host, project, args.ui_host)
        answer = ask("Sign in with your browser to fix this (Enter), or paste a personal API key (k): ").strip().lower()
        if not answer.startswith("k"):
            return sign_in_here(ws, host, project, args.ui_host)
    else:
        answer = ask("Sign in to PostHog in your browser (Enter), or paste a personal API key (k): ").strip().lower()
        if not answer.startswith("k"):
            return sign_in_here(ws, host, project, args.ui_host)
    return posthog_key(ws, args, host, project)


def sign_in_here(ws, host, project=None, ui_host=None):
    """Browser sign-in for this folder: saves the sign-in, the host and the project. Returns the project id."""
    while True:
        try:
            record = posthog_auth.sign_in(host, open_browser=open_browser)
            break
        except posthog_auth.SignInError as error:
            print(f"  {error}")
            if ask("Try again (Enter), or skip PostHog for now (s): ").strip().lower().startswith("s"):
                return None
    if not record["project"]:  # PostHog left the choice to Orion (older PostHog): ask, then keep it under that project.
        projects = PostHog(host, None, record["access_token"]).projects()
        record = posthog_auth.save(record, choose_project(projects, project))
    save_settings(ws, posthog_host=host, posthog_project=record["project"], posthog_ui_host=ui_host)
    report_access(host, record["project"])
    return record["project"]


def report_access(host, project):
    try:
        report = PostHog(host, project).access()
    except (PostHogError, OSError) as error:
        print(f"  Could not check PostHog: {error}")
        return project
    name = (report["project"] or {}).get("name")
    if report["missing"] or report["problem"]:
        print(f"  PostHog: {report['problem'] or 'missing ' + ', '.join(report['missing'])}. `orion pull` will stop until this is fixed.")
    else:
        via = "signed in" if report["via"] == "sign-in" else "personal API key"
        print(f"  PostHog ({via}): Orion can query and pull project {name or project} ({project}).")
    return project


def check_posthog_key(host, key):
    """(ok, message, missing scopes, projects) for a personal API key. The key describes its own scopes."""
    client = PostHog(host, None, key)
    try:
        current = client.request("GET", "/api/personal_api_keys/@current/")
    except PostHogAccessError as error:
        if error.status == 401:
            return False, f"PostHog did not accept the key at {host}.", [], None
        return False, str(error).replace(key, "[key]"), [], None
    except (PostHogError, OSError, ValueError) as error:
        return False, f"Could not reach PostHog at {host} ({getattr(error, 'reason', error)}).", [], None
    missing = posthog_auth.missing_scopes(current.get("scopes") or [])
    if missing:
        return (False, f"PostHog accepted the key, but it lacks {', '.join(missing)}; Orion needs {POSTHOG_SCOPES}.",
                missing, None)
    projects = client.projects()
    return True, f"PostHog accepted the key; it has the scopes Orion needs and can see {len(projects)} project(s).", [], projects


def posthog_key(ws, args, host, project, check_first=True):
    """The personal API key route. A key without the right scopes can be fixed in PostHog and checked again here."""
    held = credentials.get(POSTHOG_KEY)
    if held:
        print(f"PostHog personal API key: one is already set ({credentials.where(POSTHOG_KEY)}); press Enter to keep it.")
    else:
        print(f"Create one at {host}/settings/user-api-keys with the {POSTHOG_SCOPES} scopes.")
    typed, key, projects = None, None, None
    while check_first:
        if key is None:
            typed = ask_secret(f"PostHog personal API key (typing is hidden){' [keep]' if held else ''}: ").strip() or None
            key = typed or held
            if not key:
                print("  Skipped the PostHog personal API key.")
                return None
        if args.no_check:
            break
        ok, message, missing, projects = check_posthog_key(host, key)
        print(f"  {message}")
        if ok:
            break
        if missing:
            answer = ask("Add them to the key in PostHog, then press Enter to check again; or sign in with your browser "
                         "instead (b), paste another key (k), keep it anyway (a), or skip (s): ").strip().lower()
        else:
            answer = ask("Paste another key (k), sign in with your browser instead (b), keep it anyway (a), or skip (s) [k]: ").strip().lower() or "k"
        if answer.startswith("b"):
            return sign_in_here(ws, host, project, args.ui_host)
        if answer.startswith("k"):
            typed, key = None, None
        elif answer.startswith("a"):
            break
        elif answer.startswith("s"):
            return None
    project = args.project or choose_project(projects, ws.config().get("posthog_project"))
    save_settings(ws, posthog_host=host, posthog_project=project, posthog_ui_host=args.ui_host)
    save_keys({POSTHOG_KEY: typed})
    return project


def choose_project(projects, saved):
    """Shows the projects the key can see and asks which; Enter takes the only one, or the saved one."""
    projects = projects or []
    for p in projects:
        print(f"  {p.get('id')}  {p.get('name', '')}")
    default = str(projects[0].get("id")) if len(projects) == 1 else (str(saved) if saved else None)
    while True:
        answer = ask_default("PostHog project id", default)
        if not answer or answer.isdigit():
            return answer
        named = [str(p.get("id")) for p in projects if p.get("name") == answer]
        if len(named) == 1:
            return named[0]
        print("  Type the project's number (its id), as listed above or shown in PostHog's project settings.")


def detect_host(folder, limit=4000):
    """PostHog's EU or US cloud when the app's own code names one (env files are never read), else None."""
    skip = {"node_modules", "dist", "build", "out", "venv", "__pycache__", "vendor"}
    seen = 0
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for name in names:
            if name.startswith(".") or not name.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".html", ".vue", ".svelte", ".astro", ".py")):
                continue
            seen += 1
            if seen > limit:
                return None
            try:
                text = (Path(root) / name).read_text(errors="ignore")[:500_000]
            except OSError:
                continue
            if "eu.i.posthog.com" in text or "eu.posthog.com" in text:
                return "https://eu.posthog.com"
            if "us.i.posthog.com" in text or "us.posthog.com" in text:
                return US_HOST
    return None


# The AI labeller and TypeSafe

def setup_labeller(ws, args):
    saved = ws.config().get("llm_model")
    fixed = args.llm.strip() if args.llm else None
    if fixed:
        provider = fixed.lower() if fixed.lower() in PROVIDERS else split_model(fixed)[0]
    else:
        print("The AI labellers ask a language model to label windows from what people typed and from the replay timeline.")
        provider = choose_provider(saved)
    if provider is None:
        print("  No AI labeller for now; recipes can still use human and self-report labels.")
        return
    model = fixed if fixed and fixed.lower() not in PROVIDERS else ask_model(provider, saved)
    if not credentials.get(KEY_NAMES[provider]):
        print(f"Get a {NAMES[provider]} key at {KEY_PAGES[provider]}")
    chosen = {"provider": provider}
    check = None if args.no_check else (lambda key: check_labeller_key(key, split_model(model)[1], chosen))
    key = take_key(f"{NAMES[provider]} API key", KEY_NAMES[provider], check)
    if chosen["provider"] != provider:  # The key works only on Google's other endpoint: that one from here on.
        provider, key, model = chosen["provider"], chosen["key"], moved(model, chosen["provider"])
    save_settings(ws, llm_model=model)
    save_keys({KEY_NAMES[provider]: key})


def check_labeller_key(key, model, chosen):
    """verify, plus a second try on Google's other endpoint for a key the chosen one refuses: Google Cloud keys are
    often allowed only on Vertex AI, AI Studio keys only on the Gemini API. When the other takes the key, chosen
    switches to it and keeps the key."""
    ok, message = verify(chosen["provider"], key, model)
    other = {"gemini": "vertex", "vertex": "gemini"}.get(chosen["provider"])
    if ok is not False or not other or not verify(other, key, model)[0]:
        return ok, message
    chosen.update(provider=other, key=key)
    if other == "vertex":
        return True, (f"That is a Google Cloud key for Vertex AI, not the Gemini API. Vertex AI accepted it and {model} answered, "
                      "so Orion will run Gemini on Vertex AI.")
    return True, (f"The Gemini API accepted the key and {model} answered, so Orion will use the Gemini API. Vertex AI refused it. "
                  f"{message}, then run `orion init` again to move to Vertex AI.")


def choose_provider(saved):
    default = provider_of(saved) or next((p for p in PROVIDERS if credentials.get(KEY_NAMES[p])), PROVIDERS[0])
    while True:
        answer = ask_default("AI labeller: anthropic, openai, gemini, vertex (Gemini on Google Cloud) or skip", default).lower()
        if answer in PROVIDERS:
            return answer
        if answer == "skip":
            return None
        print("  Type anthropic, openai, gemini, vertex or skip.")


def ask_model(provider, saved):
    """The model to save; a name that does not show its provider is saved with the provider in front."""
    name = ask_default("Model", saved if provider_of(saved) == provider else DEFAULT_MODELS[provider])
    if provider_of(name) == provider or name.partition(":")[0].lower() in PROVIDERS:
        return name
    return f"{provider}:{name}"


def moved(model, provider):
    """The model's name on another provider: bare when the name shows that provider (gemini-*), else provider:name."""
    name = split_model(model)[1]
    return name if provider_of(name) == provider else f"{provider}:{name}"


def provider_of(model):
    try:
        return provider_for(model) if model else None
    except LLMSetupError:
        return None


def setup_typesafe(args):
    print("TypeSafe key for Jev (optional): ranks the self-report answers by relevance. Press Enter to skip.")
    check = None if args.no_check else verify_typesafe
    save_keys({TYPESAFE_KEY: take_key("TypeSafe API key", TYPESAFE_KEY, check)})


def take_key(label, name, check=None, hint=""):
    """Asks for a key (hidden) until its check passes or the person moves on. A key that failed for a reason fixed
    elsewhere (credit, a scope) can be checked again without typing it twice. Returns a newly typed key, else None."""
    held = credentials.get(name)
    if held:
        print(f"{label}: one is already set ({credentials.where(name)}); press Enter to keep it.")
    elif hint:
        print(hint)
    typed = key = None
    while True:
        if key is None:
            typed = ask_secret(f"{label} (typing is hidden){' [keep]' if held else ''}: ").strip() or None
            key = typed or held
            if not key:
                print(f"  Skipped the {label}.")
                return None
        if check is None:
            return typed
        ok, message = check(key)
        print(f"  {message}")
        if ok:
            return typed
        choice = ask("Paste another key (Enter), check this one again after fixing it (c), keep it anyway (k), or skip (s): ").strip().lower()
        if choice.startswith("k"):
            return typed
        if choice.startswith("s"):
            return None
        if not choice.startswith("c"):
            typed = key = None


def ask_default(label, default=None):
    answer = ask(f"{label} [{default}]: " if default else f"{label}: ").strip()
    return answer or default


def full_url(host):
    host = host.strip().rstrip("/")
    return host if "://" in host else f"https://{host}"


def save_settings(ws, **values):
    values = {k: str(v) for k, v in values.items() if v}
    if values:
        ws.save_config(**values)
        print(f"Saved {', '.join(f'{k}={v}' for k, v in values.items())} to {ws.path('config.json')}.")


def save_keys(values):
    values = {name: key for name, key in values.items() if key}
    if not values:
        return
    path = credentials.save(values)
    print(f"Saved {' and '.join(values)} to {path} (readable only by you).")
    for name in values:
        if credentials.get(name) != values[name]:
            print(f"  Note: {credentials.env_name(name)} is also set in your environment, and Orion uses that one first.")
