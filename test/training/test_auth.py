"""Signing in to PostHog in the browser (posthog_auth.py) against a local stand-in for PostHog's OAuth server: PKCE,
the project PostHog's consent page names, tokens kept out of output and readable only by you, refresh tokens that
rotate and expire, and how the PostHog client uses a sign-in next to personal API keys."""
import base64
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fakes import FakePostHog
from test_llm import FakeAPI, isolated_env
from paradigm_orion import Orion, credentials, posthog_auth
from paradigm_orion.posthog import PostHog, PostHogAccessError


class FakeOAuthPostHog:
    """PostHog's OAuth side: /oauth/authorize/ consents at once (or refuses), /oauth/token/ checks PKCE and rotates
    refresh tokens, and the API answers only to the latest access token."""

    def __init__(self, project=9, refuse=False, scope=" ".join(posthog_auth.SCOPES), scoped_teams=None, keys=None):
        self.project, self.refuse, self.scope = project, refuse, scope
        # Personal API keys: key → the scopes @current reports, one list per check (the last one repeats).
        self.keys = {key: list(scopes) for key, scopes in (keys or {}).items()}
        self.scoped_teams = [project] if scoped_teams is None else scoped_teams
        self.codes, self.access, self.refresh, self.issued, self.requests = {}, None, None, 0, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=None, location=None):
                data = json.dumps(body or {}).encode()
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                fake.requests.append(("GET", parsed.path, self.headers.get("Authorization")))
                if parsed.path == "/oauth/authorize/":
                    q = dict(urllib.parse.parse_qsl(parsed.query))
                    fake.authorize = q
                    if fake.refuse:
                        return self.reply(302, location=f"{q['redirect_uri']}?error=access_denied&state={q['state']}")
                    code = f"code-{len(fake.codes)}"
                    fake.codes[code] = q
                    return self.reply(302, location=f"{q['redirect_uri']}?code={code}&state={urllib.parse.quote(q['state'])}")
                token = (self.headers.get("Authorization") or "")[len("Bearer "):]
                if parsed.path == "/api/personal_api_keys/@current/" and token in fake.keys:
                    answers = fake.keys[token]
                    return self.reply(200, {"scopes": answers.pop(0) if len(answers) > 1 else answers[0], "scoped_teams": []})
                if token != fake.access and token not in fake.keys:
                    return self.reply(401, {"detail": "Invalid token"})
                if parsed.path == f"/api/projects/{fake.project}/":
                    return self.reply(200, {"id": fake.project, "name": "staging"})
                if parsed.path == "/api/projects/":
                    return self.reply(200, {"results": [{"id": fake.project, "name": "staging"}, {"id": 3, "name": "other"}]})
                self.reply(404)

            def do_POST(self):
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()))
                fake.requests.append(("POST", self.path, form.get("grant_type")))
                if form.get("client_id") != posthog_auth.client_id():
                    return self.reply(401, {"error": "invalid_client"})
                if form.get("grant_type") == "authorization_code":
                    asked = fake.codes.pop(form.get("code"), None)
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                    if not asked or asked["code_challenge"] != challenge or asked["redirect_uri"] != form.get("redirect_uri"):
                        return self.reply(400, {"error": "invalid_grant"})
                elif form.get("grant_type") != "refresh_token" or form.get("refresh_token") != fake.refresh:
                    return self.reply(400, {"error": "invalid_grant", "error_description": "Invalid refresh token"})
                fake.issued += 1
                fake.access, fake.refresh = f"pha_{fake.issued}", f"phr_{fake.issued}"
                self.reply(200, {"access_token": fake.access, "refresh_token": fake.refresh, "expires_in": 3600, "token_type": "Bearer",
                                 "scope": fake.scope, "scoped_teams": fake.scoped_teams, "scoped_organizations": []})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def browser(url):
    """A browser whose person clicks Authorize at once: it follows PostHog's redirects back to Orion."""
    threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=10).read(), daemon=True).start()
    return True


class SignInTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(isolated_env(self.root))

    def serve(self, **options):
        fake = FakeOAuthPostHog(**options)
        self.addCleanup(fake.close)
        return fake

    def test_sign_in_keeps_a_project_sign_in_only_you_can_read_and_never_prints_a_token(self):
        fake = self.serve()
        printed = []
        record = posthog_auth.sign_in(fake.url, open_browser=browser, timeout=10, log=printed.append)
        self.assertEqual((record["project"], record["host"]), ("9", fake.url))
        self.assertEqual(fake.authorize["required_access_level"], "project")
        self.assertEqual(fake.authorize["code_challenge_method"], "S256")
        self.assertEqual(fake.authorize["scope"].split(), list(posthog_auth.SCOPES))
        self.assertTrue(fake.authorize["redirect_uri"].startswith("http://127.0.0.1:"))
        self.assertTrue(fake.authorize["redirect_uri"].endswith("/callback"))
        path = posthog_auth.auth_file()
        self.assertEqual(path, self.root / "keys" / "posthog-auth.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(posthog_auth.login(fake.url, "9")["refresh_token"], "phr_1")
        self.assertEqual(posthog_auth.login(fake.url)["project"], "9")  # The host's only sign-in.
        self.assertTrue(any("browser" in line for line in printed))
        self.assertFalse(any("pha_" in line or "phr_" in line for line in printed))

    def test_a_refusal_or_a_reply_for_another_sign_in_does_not_sign_in(self):
        fake = self.serve(refuse=True)
        with self.assertRaisesRegex(posthog_auth.SignInError, "cancelled"):
            posthog_auth.sign_in(fake.url, open_browser=browser, timeout=10, log=lambda *a: None)
        self.assertIsNone(posthog_auth.login(fake.url))
        attempt = posthog_auth.Attempt(fake.url, "http://127.0.0.1:1/callback")
        with self.assertRaisesRegex(posthog_auth.SignInError, "another sign-in"):
            attempt.finish({"code": "x", "state": "not-this-one"})

    def test_tokens_refresh_before_they_run_out_and_the_rotated_refresh_token_is_kept(self):
        fake = self.serve()
        posthog_auth.sign_in(fake.url, open_browser=browser, timeout=10, log=lambda *a: None)
        self.assertEqual(posthog_auth.access_token(fake.url, "9"), "pha_1")  # Fresh: no refresh.
        data = json.loads(posthog_auth.auth_file().read_text())
        data["logins"][f"{fake.url} 9"]["expires_at"] = int(time.time()) + 30
        posthog_auth.auth_file().write_text(json.dumps(data))
        self.assertEqual(posthog_auth.access_token(fake.url, "9"), "pha_2")
        self.assertEqual(posthog_auth.login(fake.url, "9")["refresh_token"], "phr_2")
        self.assertEqual(posthog_auth.access_token(fake.url, "9"), "pha_2")
        self.assertEqual([r[2] for r in fake.requests if r[0] == "POST"], ["authorization_code", "refresh_token"])
        self.assertFalse(posthog_auth.auth_file().with_name("posthog-auth.json.lock").exists())
        fake.refresh = "phr_revoked"
        with self.assertRaisesRegex(posthog_auth.SignInNeeded, "orion init"):
            posthog_auth.access_token(fake.url, "9", rejected="pha_2")

    def test_a_sign_in_without_a_named_project_is_saved_under_the_one_chosen(self):
        fake = self.serve(scoped_teams=[])
        record = posthog_auth.sign_in(fake.url, open_browser=browser, timeout=10, log=lambda *a: None)
        self.assertIsNone(record["project"])
        self.assertIsNone(posthog_auth.login(fake.url))
        posthog_auth.save(record, "3")
        self.assertEqual(posthog_auth.login(fake.url, "3")["access_token"], "pha_1")


class ClientTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(isolated_env(self.root))

    def test_a_rejected_token_is_refreshed_once_and_the_request_repeated(self):
        fake = FakeOAuthPostHog()
        self.addCleanup(fake.close)
        posthog_auth.sign_in(fake.url, open_browser=browser, timeout=10, log=lambda *a: None)
        fake.access = "pha_elsewhere"  # PostHog dropped the token early.
        fake.refresh = "phr_1"
        self.assertEqual(PostHog(fake.url, "9").request("GET", "/api/projects/9/")["name"], "staging")
        self.assertEqual(posthog_auth.login(fake.url, "9")["access_token"], "pha_2")

    def test_a_key_in_the_environment_wins_then_the_sign_in_then_the_saved_key(self):
        fake = FakeOAuthPostHog()
        self.addCleanup(fake.close)
        credentials.save({"POSTHOG_PERSONAL_API_KEY": "phx_file"})
        self.assertEqual(PostHog(fake.url, "9").credential(), ("phx_file", "key"))
        posthog_auth.sign_in(fake.url, open_browser=browser, timeout=10, log=lambda *a: None)
        self.assertEqual(PostHog(fake.url, "9").credential(), ("pha_1", "sign-in"))
        self.assertEqual(PostHog(fake.url, "3").credential(), ("phx_file", "key"))  # Signed in for project 9 only.
        with mock.patch.dict(os.environ, {"POSTHOG_PERSONAL_API_KEY": "phx_env"}):
            self.assertEqual(PostHog(fake.url, "9").credential(), ("phx_env", "key"))
        self.assertEqual(PostHog(fake.url, "9", "phx_arg").credential(), ("phx_arg", "key"))

    def test_a_missing_scope_is_named_with_the_fix(self):
        fake = FakeAPI({("POST", "/api/projects/1/file_download_batch_exports/"): [
            (403, {"type": "authentication_error", "code": "permission_denied", "detail": "API key missing required scope 'batch_export:write'"})]})
        self.addCleanup(fake.close)
        with self.assertRaises(PostHogAccessError) as caught:
            PostHog(fake.url, "1", "phx_short").request("POST", "/api/projects/1/file_download_batch_exports/", {})
        message = str(caught.exception)
        self.assertIn("missing the batch_export:write scope", message)
        self.assertIn("sign in with PostHog in your browser", message)
        self.assertIn("/settings/user-api-keys", message)
        self.assertNotIn("phx_short", message)
        with self.assertRaisesRegex(PostHogAccessError, "Not signed in to PostHog: run `orion init`"):
            PostHog(fake.url, "1").request("GET", "/api/projects/1/")

    def test_pull_stops_before_creating_an_export_when_the_key_lacks_a_scope(self):
        fake = FakePostHog([], scopes=["query:read", "project:read", "batch_export:read"])
        self.addCleanup(fake.close)
        orion = Orion(posthog_project="1", host=fake.url, api_key="phx_test", home=self.root / ".orion", log=lambda *a: None)
        with self.assertRaisesRegex(PostHogAccessError, "lacks the batch_export:write scope.*orion init"):
            orion.pull("7d")
        self.assertFalse([r for r in fake.requests if r[0] == "POST"])


if __name__ == "__main__":
    unittest.main()
