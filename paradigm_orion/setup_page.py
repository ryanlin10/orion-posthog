"""`orion init` without a terminal (an agent's shell): a one-off setup page on 127.0.0.1 in the person's browser.

The page signs in to PostHog (the same browser sign-in as in a terminal) and takes the AI labeller's key and the
optional TypeSafe key, checking each one live, so a key never passes through the agent that ran `orion init` or
through a chat. Only this computer can use the page: it listens on 127.0.0.1, its address carries a random secret,
and requests that name any other host are refused (DNS rebinding). What the page saved is printed when it closes.
"""
import json
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import credentials, onboarding, posthog_auth
from .llm import DEFAULT_MODELS, KEY_NAMES, KEY_PAGES, NAMES, PROVIDERS, split_model, verify, verify_typesafe
from .posthog import US_HOST, PostHog, PostHogError

WAIT_SECONDS = 20 * 60
POSTHOG_KEY, TYPESAFE_KEY = onboarding.POSTHOG_KEY, onboarding.TYPESAFE_KEY


def ok(text):
    return {"ok": True, "text": text}


def bad(text):
    return {"ok": False, "text": text}


class Setup:
    """What the page shows and does; the HTTP handler only routes requests here."""

    def __init__(self, ws, args, port):
        self.ws, self.args, self.port = ws, args, port
        self.base = f"/s/{secrets.token_urlsafe(24)}/"
        self.attempt, self.notes, self.checking = None, {}, True  # check_saved runs as the page opens
        self.finished = threading.Event()
        saved = ws.config()
        self.host = onboarding.full_url(args.host or saved.get("posthog_host") or onboarding.detect_host(ws.root.parent) or US_HOST)
        if args.project and not saved.get("posthog_project"):
            ws.save_config(posthog_project=str(args.project))
        self.model = onboarding.model_from_flag(args.llm) if args.llm else saved.get("llm_model")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}{self.base}"

    def state(self):
        config = self.ws.config()
        provider = onboarding.provider_of(self.model) or next((p for p in PROVIDERS if credentials.get(KEY_NAMES[p])), PROVIDERS[0])
        return {
            "folder": str(self.ws.root.parent), "pending": self.checking, "finished": self.finished.is_set(),
            "posthog": {"host": self.host, "project": config.get("posthog_project"), "note": self.notes.get("posthog"),
                        "signed_in": bool(posthog_auth.login(self.host, config.get("posthog_project")))},
            "ai": {"provider": provider, "model": self.model if onboarding.provider_of(self.model) == provider else DEFAULT_MODELS[provider],
                   "defaults": DEFAULT_MODELS, "names": NAMES, "key_pages": KEY_PAGES, "note": self.notes.get("ai"),
                   "saved": {p: bool(credentials.get(KEY_NAMES[p])) for p in PROVIDERS}},
            "typesafe": {"saved": bool(credentials.get(TYPESAFE_KEY)), "note": self.notes.get("typesafe")},
        }

    def check_saved(self):
        """Checks what an earlier setup left, so the page opens on what already works and what needs fixing."""
        self.checking = True
        try:
            config = self.ws.config()
            if config.get("posthog_project") and (posthog_auth.login(self.host, config["posthog_project"]) or credentials.get(POSTHOG_KEY)):
                self.notes["posthog"] = self.check_posthog()
            provider = onboarding.provider_of(self.model)
            if provider and credentials.get(KEY_NAMES[provider]):
                passed, message = verify(provider, credentials.get(KEY_NAMES[provider]), split_model(self.model)[1])
                self.notes.setdefault("ai", {"ok": passed, "text": message})
            if credentials.get(TYPESAFE_KEY):
                passed, message = verify_typesafe(credentials.get(TYPESAFE_KEY))
                self.notes.setdefault("typesafe", {"ok": passed, "text": message})
        finally:
            self.checking = False

    def check_posthog(self):
        config = self.ws.config()
        project = config.get("posthog_project")
        try:
            report = PostHog(self.host, project, None, config.get("posthog_ui_host")).access()
        except (PostHogError, OSError) as error:
            return bad(str(error))
        via = "Signed in" if report["via"] == "sign-in" else "The saved personal API key"
        if report["missing"] or report["problem"]:
            why = report["problem"] or f"lacks {', '.join(report['missing'])}"
            return bad(f"{via} {why}, so Orion cannot pull. Sign in with PostHog to fix it.")
        name = (report["project"] or {}).get("name") or project
        return ok(f"{via}: Orion can query and pull project {name} ({project}).")

    # Actions

    def start_sign_in(self, host):
        self.host = onboarding.full_url(host or self.host)
        self.attempt = posthog_auth.Attempt(self.host, f"http://127.0.0.1:{self.port}/callback")
        return self.attempt.url

    def callback(self, params):
        """PostHog's redirect after the consent page. False for a reply that belongs to no sign-in from this page."""
        if not self.attempt or params.get("state") != self.attempt.state:
            return False
        attempt, self.attempt = self.attempt, None
        try:
            record = attempt.finish(params)
            if not record["project"]:  # PostHog left the choice to Orion: the saved project, else the only one.
                ids = [str(p.get("id")) for p in PostHog(self.host, None, record["access_token"]).projects()]
                saved = self.ws.config().get("posthog_project")
                pick = saved if saved in ids else (ids[0] if len(ids) == 1 else None)
                if not pick:
                    raise posthog_auth.SignInError("Choose a single project on PostHog's page: Orion reads one project per app")
                record = posthog_auth.save(record, pick)
            self.ws.save_config(posthog_host=self.host, posthog_project=record["project"], posthog_ui_host=self.args.ui_host)
            self.notes["posthog"] = self.check_posthog()
        except (posthog_auth.SignInError, PostHogError, OSError) as error:
            self.notes["posthog"] = bad(str(error))
        return True

    def posthog_key(self, host, key, project):
        self.host = onboarding.full_url(host or self.host)
        key = (key or "").strip() or credentials.get(POSTHOG_KEY)
        if not key:
            self.notes["posthog"] = bad(f"Paste a personal API key, made at {self.host}/settings/user-api-keys")
            return
        passed, message, missing, projects = onboarding.check_posthog_key(self.host, key)
        if not passed:
            self.notes["posthog"] = bad(message + (" Fix the key in PostHog and check again, or sign in instead." if missing else ""))
            return
        ids = [str(p.get("id")) for p in projects or []]
        project = str(project or "").strip() or self.ws.config().get("posthog_project") or (ids[0] if len(ids) == 1 else "")
        if project not in ids:
            listed = ", ".join(f"{p.get('id')} ({p.get('name')})" for p in projects or [])
            self.notes["posthog"] = bad(f"Type the id of the project this app sends to: {listed}")
            return
        if key != credentials.get(POSTHOG_KEY):
            credentials.save({POSTHOG_KEY: key})
        self.ws.save_config(posthog_host=self.host, posthog_project=project, posthog_ui_host=self.args.ui_host)
        self.notes["posthog"] = self.check_posthog()

    def ai(self, provider, model, key):
        if provider == "skip":
            self.notes["ai"] = {"ok": None, "text": "No AI labeller for now; recipes can still use self-report and human labels."}
            return
        if provider not in PROVIDERS:
            self.notes["ai"] = bad("Choose Anthropic, OpenAI or Gemini")
            return
        model = (model or "").strip() or DEFAULT_MODELS[provider]
        if onboarding.provider_of(model) != provider and model.partition(":")[0].lower() not in PROVIDERS:
            model = f"{provider}:{model}"
        key = (key or "").strip() or credentials.get(KEY_NAMES[provider])
        if not key:
            self.notes["ai"] = bad(f"Paste a {NAMES[provider]} key. Get one at {KEY_PAGES[provider]}")
            return
        try:
            passed, message = verify(provider, key, split_model(model)[1])
        except ValueError as error:
            passed, message = False, str(error)
        self.notes["ai"] = {"ok": passed, "text": message}
        if passed:
            if key != credentials.get(KEY_NAMES[provider]):
                credentials.save({KEY_NAMES[provider]: key})
            self.ws.save_config(llm_model=model)
            self.model = model

    def typesafe(self, key):
        key = (key or "").strip() or credentials.get(TYPESAFE_KEY)
        if not key:
            self.notes["typesafe"] = {"ok": None, "text": "Skipped (optional)."}
            return
        passed, message = verify_typesafe(key)
        self.notes["typesafe"] = {"ok": passed, "text": message}
        if passed and key != credentials.get(TYPESAFE_KEY):
            credentials.save({TYPESAFE_KEY: key})


def handler_for(setup, page):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def allowed(self):
            return self.headers.get("Host") == f"127.0.0.1:{setup.port}"

        def send(self, status, kind, body, headers=()):
            self.send_response(status)
            for name, value in (("Content-Type", kind), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                                ("Referrer-Policy", "no-referrer"), *headers):
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def json(self, value):
            self.send(200, "application/json", json.dumps(value).encode())

        def redirect(self, location):
            self.send(302, "text/plain", b"", (("Location", location),))

        def do_GET(self):
            if not self.allowed():
                return self.send_error(403)
            path, _, query = self.path.partition("?")
            params = dict(urllib.parse.parse_qsl(query))
            if path == "/callback":
                return self.redirect(setup.base + "#posthog") if setup.callback(params) else self.send_error(400)
            if path == setup.base:
                return self.send(200, "text/html; charset=utf-8", page)
            if path == setup.base + "state":
                return self.json(setup.state())
            if path == setup.base + "posthog/start":
                return self.redirect(setup.start_sign_in(params.get("host")))
            self.send_error(404)

        def do_POST(self):
            if not self.allowed() or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.send_error(403)
            if not self.path.startswith(setup.base):
                return self.send_error(404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self.send_error(400)
            action = self.path[len(setup.base):]
            if action == "posthog/key":
                setup.posthog_key(body.get("host"), body.get("key"), body.get("project"))
            elif action == "ai":
                setup.ai(body.get("provider"), body.get("model"), body.get("key"))
            elif action == "typesafe":
                setup.typesafe(body.get("key"))
            elif action == "done":
                setup.finished.set()
            else:
                return self.send_error(404)
            self.json(setup.state())

    return Handler


def has_browser():
    try:
        webbrowser.get()
        return True
    except webbrowser.Error:
        return False


def run(ws, args, open_browser=None, wait=None):
    if open_browser is None and not has_browser():
        print("No web browser on this computer (a remote shell?). Run `orion init` in a terminal instead: it asks its "
              "questions there, and `orion init --terminal` forces that.")
        return
    wait = wait or WAIT_SECONDS
    page = resources.files("paradigm_orion").joinpath("setup.html").read_bytes()
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    setup = Setup(ws, args, server.server_address[1])
    server.RequestHandlerClass = handler_for(setup, page)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    threading.Thread(target=setup.check_saved, daemon=True).start()
    print(f"Opened Orion's setup page in your browser: {setup.url}")
    print("  Sign in to PostHog and add the AI key there; keys stay on this computer and never pass through here. "
          f"Waiting until you press Finish (up to {wait // 60} minutes)…", flush=True)
    if not (open_browser or onboarding.open_browser)(setup.url):
        print("  The browser did not open by itself: open the address above on this computer.", flush=True)
    try:
        finished = setup.finished.wait(wait)
    except KeyboardInterrupt:
        finished = False
    finally:
        server.shutdown()
        server.server_close()
    for label, key in (("PostHog", "posthog"), ("AI labeller", "ai"), ("TypeSafe", "typesafe")):
        note = setup.notes.get(key)
        mark = {True: "ok", False: "needs attention", None: "skipped"}[note["ok"]] if note else "not set up"
        detail = f" ({note['text']})" if note else ""
        print(f"{label}: {mark}{detail}")
    if not finished:
        print("The setup page was closed without Finish; what it saved is kept. Run `orion init` again to continue.")
    print("`orion check` checks everything again at any time.")
