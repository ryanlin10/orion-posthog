"""`orion init`, the setup step after installing (pip cannot ask questions during an install).

In a terminal it asks for the PostHog host, personal API key and project, the AI labeller and its key, and an
optional TypeSafe key, checking each key as it goes. Settings are saved to .orion/config.json and keys to the keys
file outside the project (see credentials.py). Keys are typed hidden and never printed.
"""
import getpass
import json
import sys

from . import credentials
from .llm import DEFAULT_MODELS, KEY_NAMES, NAMES, PROVIDERS, LLMSetupError, provider_for, split_model, verify, verify_typesafe
from .posthog import US_HOST, PostHog, PostHogError
from .workspace import Workspace

# The prompts; tests replace them.
ask = input
ask_secret = getpass.getpass

POSTHOG_KEY, TYPESAFE_KEY = "POSTHOG_PERSONAL_API_KEY", "TYPESAFE_API_KEY"
POSTHOG_SCOPES = "project:read, query:read, batch_export:read and batch_export:write"


def init(args):
    ws = Workspace()
    if args.no_input or not sys.stdin.isatty():
        return save_flags(ws, args)
    print(f"Setting up Orion for {ws.root}. Press Enter to accept the value in [brackets].")
    try:
        setup_posthog(ws, args)
        setup_labeller(ws, args)
        setup_typesafe(args)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Anything saved above is kept; run `orion init` again to finish.")
        return
    print("Setup done. Next, `orion pull` exports your telemetry from PostHog.")


def save_flags(ws, args):
    """No questions (no terminal, or --no-input): save the flags as given."""
    model = model_from_flag(args.llm) if args.llm else None
    stored = ws.save_config(posthog_host=args.host, posthog_project=args.project, posthog_ui_host=args.ui_host, llm_model=model)
    print(f"Saved {ws.path('config.json')}: {json.dumps(stored)}")
    found = [name for name in credentials.NAMES if credentials.get(name)]
    print(f"Keys found: {', '.join(found) or 'none'}. Orion reads them from the environment or {credentials.keys_file()}; "
          "run `orion init` in a terminal to save them there.")


def model_from_flag(value):
    """--llm takes a provider (meaning its default model) or a model name, which is saved as given."""
    value = value.strip()
    if value.lower() in PROVIDERS:
        return DEFAULT_MODELS[value.lower()]
    split_model(value)  # Fails now on a name no provider claims.
    return value


def setup_posthog(ws, args):
    saved = ws.config()
    host = full_url(args.host or ask_default("PostHog host", saved.get("posthog_host") or US_HOST))
    found = {}
    check = None if args.no_check else (lambda key: check_posthog(host, key, found))
    key = take_key("PostHog personal API key", POSTHOG_KEY, check,
                   f"Create one at {host}/settings/user-api-keys with the {POSTHOG_SCOPES} scopes.")
    project = args.project or choose_project(found.get("projects"), saved.get("posthog_project"))
    save_settings(ws, posthog_host=host, posthog_project=project, posthog_ui_host=args.ui_host)
    save_keys({POSTHOG_KEY: key})


def check_posthog(host, key, found):
    """Lists the projects the key can see, which proves the key and the host work together."""
    client = PostHog(host, None, key)
    try:
        found["projects"] = client.projects()
    except PostHogError as error:
        if "HTTP 403" in str(error):
            return True, "PostHog accepted the key, but it cannot list projects (give it the project:read scope to list them here)."
        return False, f"PostHog did not accept the key at {host} ({str(error).replace(key, '[key]')})."
    except (OSError, ValueError) as error:
        return False, f"Could not reach PostHog at {host} ({getattr(error, 'reason', error)})."
    return True, f"PostHog accepted the key; it can see {len(found['projects'])} project(s)."


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
    name = KEY_NAMES[provider]
    check = None if args.no_check else (lambda key: verify(provider, key))
    key = take_key(f"{NAMES[provider]} API key", name, check)
    model = fixed if fixed and fixed.lower() not in PROVIDERS else ask_model(provider, saved)
    save_settings(ws, llm_model=model)
    save_keys({name: key})


def choose_provider(saved):
    default = provider_of(saved) or next((p for p in PROVIDERS if credentials.get(KEY_NAMES[p])), PROVIDERS[0])
    while True:
        answer = ask_default("AI labeller: anthropic, openai, gemini or skip", default).lower()
        if answer in PROVIDERS:
            return answer
        if answer == "skip":
            return None
        print("  Type anthropic, openai, gemini or skip.")


def ask_model(provider, saved):
    """The model to save; a name that does not show its provider is saved with the provider in front."""
    name = ask_default("Model", saved if provider_of(saved) == provider else DEFAULT_MODELS[provider])
    if provider_of(name) == provider or name.partition(":")[0].lower() in PROVIDERS:
        return name
    return f"{provider}:{name}"


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
    """Asks for a key (hidden) until its check passes or the person moves on. Returns a newly typed key, else None."""
    held = credentials.get(name)
    if held:
        print(f"{label}: one is already set ({credentials.where(name)}); press Enter to keep it.")
    elif hint:
        print(hint)
    while True:
        typed = ask_secret(f"{label} (typing is hidden){' [keep]' if held else ''}: ").strip()
        key = typed or held
        if not key:
            print(f"  Skipped the {label}.")
            return None
        if check is None:
            return typed or None
        ok, message = check(key)
        print(f"  {message}")
        if ok:
            return typed or None
        choice = ask("Try another key (Enter), keep this one anyway (k), or skip (s): ").strip().lower()
        if choice.startswith("k"):
            return typed or None
        if choice.startswith("s"):
            return None


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
