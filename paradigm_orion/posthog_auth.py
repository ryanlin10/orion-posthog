"""Signing in to PostHog in the browser, so nobody pastes a personal API key or has to pick its scopes.

Orion is a public OAuth client (PKCE, no secret) whose client_id is the address of its client metadata document,
the way PostHog's own command-line tools sign in. PostHog's consent page asks the person to pick one project and
then sends the browser to 127.0.0.1 with a code, which buys an access token (good for an hour) and a refresh token
(replaced on every use, dropped after 30 days unused). The tokens always carry exactly the scopes Orion needs.

Sign-ins are kept in posthog-auth.json beside the keys file, readable only by you, and are never printed. A refresh
happens under a lock and re-reads the file first, so two Orion processes never spend the same refresh token.
"""
import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import credentials

CLIENT_ID = "https://cdn.paradigmmachines.ai/orion/oauth/client-metadata.json"
SCOPES = ("project:read", "query:read", "batch_export:read", "batch_export:write")
REFRESH_MARGIN = 120  # Seconds of life left below which a token is refreshed before use.


class SignInError(RuntimeError):
    pass


class SignInNeeded(SignInError):
    """The saved sign-in no longer works (refresh token expired or revoked); signing in again fixes it."""


def client_id():
    return os.environ.get("ORION_POSTHOG_CLIENT_ID") or CLIENT_ID


def auth_file():
    if os.environ.get("ORION_POSTHOG_AUTH_FILE"):
        return Path(os.environ["ORION_POSTHOG_AUTH_FILE"]).expanduser()
    return credentials.keys_file().parent / "posthog-auth.json"


def _read():
    try:
        return json.loads(auth_file().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _write(data):
    path = auth_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials.write_private(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


@contextmanager
def _locked(timeout=30):
    """A lock other Orion processes (Python or the setup wizard) respect: a folder only one of them can create."""
    lock = auth_file().with_name(auth_file().name + ".lock")
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 60:  # Left behind by a process that crashed.
                    lock.rmdir()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise SignInError(f"Another Orion process is holding {lock}; try again in a moment") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except FileNotFoundError:
            pass


def _key(host, project):
    return f"{host.rstrip('/')} {project}"


def login(host, project=None):
    """The saved sign-in for this host and project; with no project, the host's only one. None when there is none."""
    logins = _read().get("logins", {})
    if project:
        return logins.get(_key(host, project))
    found = [record for record in logins.values() if record.get("host") == host.rstrip("/")]
    return found[0] if len(found) == 1 else None


def save(record, project=None):
    """Keeps a sign-in (for `project` when PostHog left the choice of project to Orion). Returns the record."""
    record = {**record, "project": str(project or record.get("project") or "") or None}
    if not record["project"]:
        raise SignInError("A PostHog sign-in needs a project")
    with _locked():
        data = _read()
        data.setdefault("logins", {})[_key(record["host"], record["project"])] = record
        _write(data)
    return record


class TokenError(SignInError):
    def __init__(self, status, code, description):
        super().__init__(f"PostHog refused the token request (HTTP {status}{f', {code}' if code else ''}"
                         f"{f': {description}' if description else ''})")
        self.status, self.code = status, code


def _token_request(host, fields):
    data = urllib.parse.urlencode({**fields, "client_id": client_id()}).encode()
    req = urllib.request.Request(f"{host}/oauth/token/", data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read() or b"{}")
        except ValueError:
            body = {}
        raise TokenError(error.code, body.get("error"), body.get("error_description")) from None
    except (OSError, ValueError) as error:
        raise SignInError(f"Could not reach PostHog at {host} ({getattr(error, 'reason', error)})") from None


def _record(host, reply, previous=None):
    previous = previous or {}
    teams = reply.get("scoped_teams") or []
    return {"host": host, "project": str(teams[0]) if len(teams) == 1 else previous.get("project"),
            "access_token": reply["access_token"], "refresh_token": reply.get("refresh_token") or previous.get("refresh_token"),
            "expires_at": int(time.time()) + int(reply.get("expires_in") or 3600),
            "scope": reply.get("scope") or previous.get("scope") or " ".join(SCOPES), "client_id": client_id()}


def _fresh(record, rejected):
    return record["access_token"] != rejected and record["expires_at"] - time.time() > REFRESH_MARGIN


def access_token(host, project, rejected=None):
    """A working access token for the saved sign-in, refreshed first when it is nearly used up or was just rejected.
    None when there is no sign-in; SignInNeeded when PostHog no longer honours it."""
    record = login(host, project)
    if record is None or _fresh(record, rejected):
        return record and record["access_token"]
    with _locked():
        data = _read()  # Another process may have refreshed it meanwhile.
        record = data.get("logins", {}).get(_key(host, project))
        if record is None or _fresh(record, rejected):
            return record and record["access_token"]
        try:
            reply = _token_request(record["host"], {"grant_type": "refresh_token", "refresh_token": record["refresh_token"]})
        except TokenError as error:
            if error.status in (400, 401, 403):
                raise SignInNeeded(f"The PostHog sign-in for project {project} has ended ({error.code or error.status}). "
                                   "Run `orion init` to sign in again; it takes one click in your browser.") from None
            raise
        data["logins"][_key(host, project)] = record = _record(record["host"], reply, record)
        _write(data)
        return record["access_token"]


class Attempt:
    """One sign-in: the PKCE pair, the state and the redirect address. finish() trades PostHog's code for tokens."""

    def __init__(self, host, redirect_uri, scopes=SCOPES):
        self.host, self.redirect_uri = host.rstrip("/"), redirect_uri
        self.verifier, self.state = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        self.url = f"{self.host}/oauth/authorize/?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": client_id(), "redirect_uri": redirect_uri, "scope": " ".join(scopes),
            "state": self.state, "code_challenge": challenge, "code_challenge_method": "S256",
            "required_access_level": "project"})  # PostHog's page asks for one project, which names the project here.

    def finish(self, params):
        """The tokens for PostHog's redirect parameters, saved when they name a project. Raises SignInError."""
        if params.get("state") != self.state:
            raise SignInError("PostHog's reply belongs to another sign-in; start again")
        if params.get("error"):
            reason = params.get("error_description") or params["error"]
            raise SignInError("PostHog sign-in was cancelled" if params["error"] == "access_denied" else f"PostHog sign-in failed: {reason}")
        if not params.get("code"):
            raise SignInError("PostHog's reply had no code; start again")
        reply = _token_request(self.host, {"grant_type": "authorization_code", "code": params["code"],
                                           "redirect_uri": self.redirect_uri, "code_verifier": self.verifier})
        record = _record(self.host, reply)
        return save(record) if record["project"] else record


def result_page(title, message, ok=True):
    """The small page the browser shows after PostHog sends it back."""
    mark = "✓" if ok else "!"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orion · {html.escape(title)}</title><style>
:root {{ color-scheme: light dark; --bg: #f7f7f5; --fg: #151515; --muted: #6b6b66; --line: #e2e2dc; --mark: {'#1f7a4d' if ok else '#a35a00'}; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #111110; --fg: #f2f2ee; --muted: #9a9a92; --line: #2b2b28; --mark: {'#5cc28f' if ok else '#e0a050'}; }} }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, sans-serif; }}
main {{ max-width: 420px; padding: 32px 24px; border: 1px solid var(--line); border-radius: 12px; margin: 16px; }}
b {{ color: var(--mark); font-size: 20px; }} p {{ color: var(--muted); margin: 8px 0 0; }}
</style></head><body><main><b>{mark}</b> <strong>{html.escape(title)}</strong><p>{html.escape(message)}</p></main></body></html>"""


def sign_in(host, open_browser=webbrowser.open, timeout=600, log=print):
    """Signs in through the browser and returns the saved sign-in (its "project" is None when PostHog left the choice
    to Orion; save() it with the project then). Raises SignInError when PostHog says no or nobody answers in time."""
    outcome = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/callback" or done.is_set():
                self.send_error(404)
                return
            params = dict(urllib.parse.parse_qsl(parsed.query))
            try:
                outcome["record"] = attempt.finish(params)
                page = result_page("Signed in to PostHog", "Orion can now read this project. You can close this tab.")
            except SignInError as error:
                if params.get("state") != attempt.state:  # Not our sign-in (an old tab): ignore it.
                    self.send_error(400)
                    return
                outcome["error"] = error
                page = result_page("Not signed in", str(error), ok=False)
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    attempt = Attempt(host, f"http://127.0.0.1:{server.server_address[1]}/callback")
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    try:
        log("Opening PostHog in your browser: sign in, choose the project this app sends to, and click Authorize.")
        log(f"  If no browser opens, open this address on this computer: {attempt.url}")
        open_browser(attempt.url)
        if not done.wait(timeout):
            raise SignInError(f"PostHog did not answer within {timeout // 60} minutes; run `orion init` to try again")
    finally:
        server.shutdown()
        server.server_close()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["record"]
