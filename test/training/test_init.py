"""`orion init` and where keys live: the keys file outside the project (environment first, mode 0600); setup in a
terminal (browser sign-in to PostHog, or a personal API key whose scopes are checked) and on the setup page an agent
opens, against local stand-ins for PostHog, OpenAI and TypeSafe; the no-questions mode; `orion check`; and the folder
Orion works in (never the home folder; found from below like git)."""
import argparse
import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from test_auth import FakeOAuthPostHog, browser
from test_llm import FakeAPI, isolated_env, keyed
from paradigm_orion import credentials, onboarding, posthog_auth, setup_page
from paradigm_orion.cli import main
from paradigm_orion.posthog import PostHog
from paradigm_orion.workspace import Workspace


def bearer(key):
    return f"Bearer {key}"


class Script:
    """Answers prompts in order and remembers what was asked."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"Unexpected question: {prompt}")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Terminal(io.StringIO):
    def __init__(self, tty=True):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(isolated_env(self.root))
        self.file = self.root / "keys" / "keys.env"

    def test_keys_file_is_outside_the_project(self):
        self.assertEqual(credentials.keys_file(), self.file)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}, clear=True):
            self.assertEqual(credentials.keys_file(), self.root / "xdg" / "orion" / "keys.env")
        with mock.patch.dict(os.environ, {"HOME": str(self.root / "home")}, clear=True):
            self.assertEqual(credentials.keys_file(), self.root / "home" / ".config" / "orion" / "keys.env")

    def test_environment_wins_over_the_file_and_google_key_counts_for_gemini(self):
        self.assertIsNone(credentials.get("OPENAI_API_KEY"))
        credentials.save({"OPENAI_API_KEY": "sk-file", "GEMINI_API_KEY": "AIza-file"})
        self.assertEqual(credentials.get("OPENAI_API_KEY"), "sk-file")
        self.assertEqual(credentials.where("OPENAI_API_KEY"), str(self.file))
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env", "GOOGLE_API_KEY": "AIza-env"}):
            self.assertEqual(credentials.get("OPENAI_API_KEY"), "sk-env")
            self.assertEqual(credentials.get("GEMINI_API_KEY"), "AIza-env")
            self.assertEqual(credentials.where("GEMINI_API_KEY"), "environment variable GOOGLE_API_KEY")
        self.file.write_text("GOOGLE_API_KEY=AIza-alias\n")
        self.assertEqual(credentials.get("GEMINI_API_KEY"), "AIza-alias")
        self.assertIsNone(credentials.where("TYPESAFE_API_KEY"))

    def test_save_merges_into_a_file_only_you_can_read(self):
        self.file.parent.mkdir(mode=0o755)
        self.file.write_text("# my keys\n\nexport POSTHOG_PERSONAL_API_KEY=\"phx_old\"\nANTHROPIC_API_KEY=sk-ant-1  # work\nOTHER=x\n")
        self.assertEqual(credentials.read(), {"POSTHOG_PERSONAL_API_KEY": "phx_old", "ANTHROPIC_API_KEY": "sk-ant-1", "OTHER": "x"})
        self.assertEqual(credentials.save({"POSTHOG_PERSONAL_API_KEY": "phx_new", "OPENAI_API_KEY": "sk-2", "GEMINI_API_KEY": None}), self.file)
        self.assertEqual(self.file.read_text(), "# my keys\n\nPOSTHOG_PERSONAL_API_KEY=phx_new\nANTHROPIC_API_KEY=sk-ant-1  # work\nOTHER=x\nOPENAI_API_KEY=sk-2\n")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.file.parent.iterdir()), ["keys.env"])
        with self.assertRaises(ValueError):
            credentials.save({"OPENAI_API_KEY": "two\nlines"})
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}, clear=True):
            path = credentials.save({"TYPESAFE_API_KEY": "ts-1"})
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(credentials.get("TYPESAFE_API_KEY"), "ts-1")

    def test_posthog_key_comes_from_the_argument_then_environment_then_file(self):
        self.assertIsNone(PostHog("http://127.0.0.1:9", "1").api_key)
        credentials.save({"POSTHOG_PERSONAL_API_KEY": "phx_file"})
        self.assertEqual(PostHog("http://127.0.0.1:9", "1").api_key, "phx_file")
        with mock.patch.dict(os.environ, {"POSTHOG_PERSONAL_API_KEY": "phx_env"}):
            self.assertEqual(PostHog("http://127.0.0.1:9", "1").api_key, "phx_env")
            self.assertEqual(PostHog("http://127.0.0.1:9", "1", "phx_arg").api_key, "phx_arg")


class InitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()  # The workspace prints resolved paths (/private/var on macOS).
        self.keys = self.root / "keys" / "keys.env"
        self.config = self.root / ".orion" / "config.json"

    def serve(self, routes):
        fake = FakeAPI(routes)
        self.addCleanup(fake.close)
        return fake

    def posthog(self, **options):
        fake = FakeOAuthPostHog(**options)
        self.addCleanup(fake.close)
        return fake

    def run_init(self, argv, asked=(), typed=(), tty=True, **env):
        """Runs `orion init` with scripted answers; returns (exit code, what it printed, the two scripts)."""
        ask, ask_secret = Script(*asked), Script(*typed)
        out, err = io.StringIO(), io.StringIO()
        with isolated_env(self.root, ORION_HOME=str(self.root / ".orion"), **env), \
                mock.patch.object(onboarding, "ask", ask), mock.patch.object(onboarding, "ask_secret", ask_secret), \
                mock.patch.object(onboarding, "open_browser", browser), \
                mock.patch("sys.stdin", Terminal(tty)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["init", *argv])
        self.assertEqual((ask.answers, ask_secret.answers), ([], []), "every scripted answer is used")
        return code, out.getvalue() + err.getvalue(), ask, ask_secret

    def saved_keys(self):
        with isolated_env(self.root):
            return credentials.read()

    def signed_in(self, host, project):
        with isolated_env(self.root):
            return posthog_auth.login(host, project)

    def test_signing_in_with_the_browser_sets_up_posthog_with_nothing_to_paste(self):
        fake = self.posthog()
        code, printed, ask, _ = self.run_init([], asked=[fake.url, "", "skip"], typed=[""])
        self.assertEqual(code, 0, printed)
        self.assertEqual(json.loads(self.config.read_text()), {"posthog_host": fake.url, "posthog_project": "9"})
        self.assertEqual(ask.prompts[1], "Sign in to PostHog in your browser (Enter), or paste a personal API key (k): ")
        self.assertIn("Opening PostHog in your browser", printed)
        self.assertIn("Orion can query and pull project staging (9)", printed)
        self.assertEqual(self.signed_in(fake.url, "9")["scope"].split(), list(posthog_auth.SCOPES))
        self.assertEqual(self.saved_keys(), {})  # Nothing pasted, nothing in the keys file.
        self.assertNotIn("pha_", printed)
        self.assertNotIn("phr_", printed)

    def test_a_saved_key_without_the_export_scopes_is_fixed_by_signing_in(self):
        """The case that used to fail only at `orion pull`: a key that lists projects but cannot export."""
        fake = self.posthog(keys={"phx_short": [["project:read", "query:read"]]})
        self.keys.parent.mkdir()
        self.keys.write_text("POSTHOG_PERSONAL_API_KEY=phx_short\n")
        code, printed, ask, _ = self.run_init([], asked=[fake.url, "", "skip"], typed=[""])
        self.assertEqual(code, 0, printed)
        self.assertIn("PostHog accepted the key, but it lacks batch_export:read, batch_export:write", printed)
        self.assertEqual(ask.prompts[1], "Sign in with your browser to fix this (Enter), or paste a personal API key (k): ")
        self.assertIn("Orion can query and pull project staging (9)", printed)
        with isolated_env(self.root):
            self.assertEqual(PostHog(fake.url, "9").credential()[1], "sign-in")  # The sign-in wins over the saved key.

    def test_a_pasted_key_missing_scopes_is_checked_again_without_pasting_it_twice(self):
        posthog = self.posthog(keys={"phx_k": [["query:read"], ["*"]]})
        openai = self.serve({("POST", "/v1/responses"): [keyed("authorization", "sk-good", {"output": []}, value=bearer)]})
        typesafe = self.serve({("POST", "/v1/systemone"): [(401, {"error": "unknown key"})]})
        code, printed, ask, ask_secret = self.run_init(
            [], asked=[posthog.url, "k", "", "", "staging", "openai", "", "k"], typed=["phx_wrong", "phx_k", "sk-good", "ts-unchecked"],
            ORION_OPENAI_URL=openai.url, ORION_TYPESAFE_URL=f"{typesafe.url}/v1/systemone")
        self.assertEqual(code, 0, printed)
        self.assertEqual(json.loads(self.config.read_text()), {"posthog_host": posthog.url, "posthog_project": "9", "llm_model": "gpt-5.6-sol"})
        self.assertEqual(self.saved_keys(), {"POSTHOG_PERSONAL_API_KEY": "phx_k", "OPENAI_API_KEY": "sk-good", "TYPESAFE_API_KEY": "ts-unchecked"})
        self.assertEqual(stat.S_IMODE(self.keys.stat().st_mode), 0o600)
        self.assertIn("PostHog did not accept the key", printed)
        self.assertIn("lacks project:read, batch_export:read, batch_export:write", printed)
        self.assertEqual(ask.prompts[3], "Add them to the key in PostHog, then press Enter to check again; or sign in with your browser "
                                         "instead (b), paste another key (k), keep it anyway (a), or skip (s): ")
        self.assertIn("it has the scopes Orion needs and can see 2 project(s)", printed)
        self.assertIn("  9  staging\n  3  other\n", printed)
        self.assertIn("OpenAI accepted the key and gpt-5.6-sol answered", printed)
        self.assertIn("TypeSafe did not accept the key", printed)
        self.assertEqual(len(ask_secret.prompts), 4)  # phx_k was typed once, and checked twice.
        self.assertTrue(all("hidden" in p for p in ask_secret.prompts))
        self.assertEqual(ask.prompts[0], "PostHog host [https://us.posthog.com]: ")
        self.assertEqual(ask.prompts[6], "Model [gpt-5.6-sol]: ")
        for key in ("phx_wrong", "phx_k", "sk-good", "ts-unchecked"):
            self.assertNotIn(key, printed)
            self.assertNotIn(key, self.config.read_text())

    def test_an_ai_key_is_checked_on_the_model_it_will_use_and_can_be_checked_again(self):
        answers = [(400, {"error": {"message": "Your credit balance is too low to access the Anthropic API."}}),
                   (200, {"content": [{"type": "text", "text": "OK"}]})]
        anthropic = self.serve({("POST", "/v1/messages"): answers})
        code, printed, ask, ask_secret = self.run_init(["--host", "http://127.0.0.1:9", "--project", "3"], asked=["k", "anthropic", "claude-x", "c"],
                                                       typed=["", "sk-ant-1", ""], ORION_ANTHROPIC_URL=anthropic.url)
        self.assertEqual(code, 0, printed)
        self.assertIn("Anthropic accepted the key, but its account cannot pay for requests. Add credit at https://console.anthropic.com/settings/billing", printed)
        self.assertIn("Anthropic accepted the key and claude-x answered", printed)
        self.assertEqual([r["body"]["model"] for r in anthropic.requests], ["claude-x", "claude-x"])
        self.assertEqual(anthropic.requests[0]["body"]["max_tokens"], 1)
        self.assertEqual(self.saved_keys(), {"ANTHROPIC_API_KEY": "sk-ant-1"})
        self.assertEqual(len(ask_secret.prompts), 3)  # The key was typed once.

    def test_a_google_cloud_key_given_for_gemini_switches_to_vertex_ai(self):
        google = self.serve(google_key("AQ.cloud", "vertex", "gemini-3.8-flash", "gemini-3.8-pro"))
        urls = {"ORION_GEMINI_URL": google.url, "ORION_VERTEX_URL": google.url}
        code, printed, ask, _ = self.run_init(["--host", "http://127.0.0.1:9", "--project", "3"], asked=["k", "gemini", ""],
                                              typed=["", "AQ.cloud", ""], **urls)
        self.assertEqual(code, 0, printed)
        self.assertIn("That is a Google Cloud key for Vertex AI, not the Gemini API. Vertex AI accepted it and gemini-3.8-flash "
                      "answered, so Orion will run Gemini on Vertex AI.", printed)
        self.assertEqual(ask.prompts[2], "Model [gemini-3.8-flash]: ")
        self.assertEqual(json.loads(self.config.read_text())["llm_model"], "vertex:gemini-3.8-flash")
        self.assertEqual(self.saved_keys(), {"VERTEX_API_KEY": "AQ.cloud"})
        self.assertNotIn("AQ.cloud", printed)
        # A model given with --llm moves to Vertex AI with the key.
        code, printed, _, _ = self.run_init(["--host", "http://127.0.0.1:9", "--project", "3", "--llm", "gemini-3.8-pro"],
                                            asked=["k"], typed=["", "AQ.cloud", ""], **urls)
        self.assertEqual((code, json.loads(self.config.read_text())["llm_model"]), (0, "vertex:gemini-3.8-pro"))

    def test_a_gemini_api_key_given_for_vertex_ai_stays_on_the_gemini_api(self):
        google = self.serve(google_key("AQ.studio", "gemini", "gemini-3.8-flash"))
        code, printed, ask, _ = self.run_init(["--host", "http://127.0.0.1:9", "--project", "3"], asked=["k", "vertex", ""],
                                              typed=["", "AQ.studio", ""], ORION_GEMINI_URL=google.url, ORION_VERTEX_URL=google.url)
        self.assertEqual(code, 0, printed)
        self.assertIn("The Gemini API accepted the key and gemini-3.8-flash answered, so Orion will use the Gemini API. Vertex AI "
                      "refused it. The key's API restrictions leave out Vertex AI, so Google blocks it there. Add "
                      "aiplatform.googleapis.com to them at https://console.cloud.google.com/apis/credentials, then run `orion init` "
                      "again to move to Vertex AI.", printed)
        self.assertEqual(ask.prompts[2], "Model [vertex:gemini-3.8-flash]: ")
        self.assertEqual(json.loads(self.config.read_text())["llm_model"], "gemini-3.8-flash")
        self.assertEqual(self.saved_keys(), {"GEMINI_API_KEY": "AQ.studio"})
        self.assertNotIn("AQ.studio", printed)

    def test_saved_keys_are_kept_with_enter(self):
        self.keys.parent.mkdir()
        self.keys.write_text("POSTHOG_PERSONAL_API_KEY=phx_saved\nANTHROPIC_API_KEY=sk-ant-saved\n")
        before = self.keys.read_bytes()
        code, printed, _, _ = self.run_init(["--project", "42", "--no-check"], asked=["http://127.0.0.1:9", "k", "", ""], typed=["", "", ""])
        self.assertEqual(code, 0, printed)
        self.assertEqual(json.loads(self.config.read_text()), {"posthog_host": "http://127.0.0.1:9", "posthog_project": "42", "llm_model": "claude-sonnet-5-5"})
        self.assertEqual(self.keys.read_bytes(), before)
        self.assertIn(f"PostHog personal API key: one is already set ({self.keys}); press Enter to keep it.", printed)
        self.assertIn("Skipped the TypeSafe API key", printed)
        self.assertNotIn("readable only by you", printed)  # Nothing new to write to the keys file.

    def test_llm_flag_skips_the_questions_it_answers_and_notes_a_key_the_environment_overrides(self):
        code, printed, ask, _ = self.run_init(["--host", "http://127.0.0.1:9", "--project", "3", "--llm", "openai:gpt-5.6-mini", "--no-check"],
                                              asked=["k"], typed=["phx_typed", "sk-typed", ""], OPENAI_API_KEY="sk-env")
        self.assertEqual(code, 0, printed)
        self.assertEqual(ask.prompts, ["Sign in to PostHog in your browser (Enter), or paste a personal API key (k): "])
        self.assertEqual(json.loads(self.config.read_text())["llm_model"], "openai:gpt-5.6-mini")
        self.assertEqual(self.saved_keys(), {"POSTHOG_PERSONAL_API_KEY": "phx_typed", "OPENAI_API_KEY": "sk-typed"})
        self.assertIn("OPENAI_API_KEY is also set in your environment, and Orion uses that one first", printed)
        self.assertNotIn("sk-env", printed)

    def test_stopping_part_way_keeps_what_was_saved(self):
        code, printed, _, _ = self.run_init(["--host", "http://127.0.0.1:9", "--project", "3", "--no-check"],
                                            asked=["k", KeyboardInterrupt()], typed=["phx_typed"])
        self.assertEqual(code, 0)
        self.assertIn("Stopped", printed)
        self.assertEqual(self.saved_keys(), {"POSTHOG_PERSONAL_API_KEY": "phx_typed"})
        self.assertNotIn("llm_model", json.loads(self.config.read_text()))

    def test_no_input_saves_the_flags_as_given(self):
        code, printed, _, _ = self.run_init(["--host", "https://eu.posthog.com", "--project", "5", "--llm", "openai", "--no-input"],
                                            OPENAI_API_KEY="sk-env")
        self.assertEqual(code, 0, printed)
        self.assertEqual(json.loads(self.config.read_text()), {"posthog_host": "https://eu.posthog.com", "posthog_project": "5", "llm_model": "gpt-5.6-sol"})
        self.assertIn("Keys found: OPENAI_API_KEY.", printed)
        self.assertIn("run `orion init` without --no-input", printed)
        self.assertNotIn("sk-env", printed)
        code, printed, _, _ = self.run_init(["--llm", "gemini:gemini-3.8-pro", "--no-input"], tty=False)
        self.assertEqual((code, json.loads(self.config.read_text())["llm_model"]), (0, "gemini:gemini-3.8-pro"))
        self.assertIn("Keys found: none.", printed)
        code, printed, _, _ = self.run_init(["--llm", "llama-3", "--no-input"])
        self.assertEqual(code, 1)
        self.assertIn("orion: Cannot tell which provider serves the model 'llama-3'", printed)

    def test_the_eu_cloud_is_the_default_when_the_app_sends_there(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "analytics.ts").write_text("posthog.init('phc_x', { api_host: 'https://eu.i.posthog.com' })\n")
        (self.root / ".env").write_text("POSTHOG_HOST=https://us.posthog.com\n")  # Never read.
        self.assertEqual(onboarding.detect_host(self.root), "https://eu.posthog.com")
        (self.root / "src" / "analytics.ts").unlink()
        self.assertIsNone(onboarding.detect_host(self.root))


def google_key(key, allowed, *models):
    """Routes for a Google key whose API restrictions allow only one of Google's endpoints: "vertex" or "gemini"."""
    paths = {"gemini": "/v1beta/models", "vertex": "/v1beta1/publishers/google/models"}
    blocked = (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Requests to this API are blocked.",
                               "details": [{"reason": "API_KEY_SERVICE_BLOCKED"}]}})
    answer = {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}}]}
    return {("POST", f"{path}/{m}:generateContent"): [keyed("x-goog-api-key", key, answer) if provider == allowed else blocked]
            for provider, path in paths.items() for m in models}


class SetupPageTests(unittest.TestCase):
    """`orion init` without a terminal: the person signs in and adds keys on a page in their own browser."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.config = self.root / ".orion" / "config.json"

    def test_the_page_signs_in_checks_keys_and_saves_only_what_works(self):
        posthog = FakeOAuthPostHog()
        openai = FakeAPI({("POST", "/v1/responses"): [keyed("authorization", "sk-good", {"output": []}, value=bearer)]})
        self.addCleanup(posthog.close)
        self.addCleanup(openai.close)
        seen = {}

        def call(url, body=None, host=None):
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json", **({"Host": host} if host else {})}
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=10) as reply:
                    return reply.status, reply.read()
            except urllib.error.HTTPError as error:
                return error.code, b""

        def person(url):
            """Someone at the browser: signs in, pastes a wrong key and then the right one, and presses Finish."""
            def act():
                prefix = url.split("/s/")[0]
                seen["foreign"] = call(url + "state", host="attacker.example")[0]
                seen["guessed"] = call(f"{prefix}/s/guess/state")[0]
                seen["page"] = call(url)[1]
                call(f"{url}posthog/start?host={posthog.url}")  # → PostHog's consent → back to the page.
                seen["wrong"] = json.loads(call(url + "ai", {"provider": "openai", "model": "", "key": "sk-wrong"})[1])["ai"]["note"]
                seen["right"] = json.loads(call(url + "ai", {"provider": "openai", "model": "", "key": "sk-good"})[1])["ai"]["note"]
                call(url + "done", {})
            threading.Thread(target=act, daemon=True).start()
            return True

        out = io.StringIO()
        with isolated_env(self.root, ORION_HOME=str(self.root / ".orion"), ORION_OPENAI_URL=openai.url), \
                mock.patch.object(onboarding, "open_browser", person), mock.patch.object(setup_page, "has_browser", lambda: True), \
                mock.patch.object(setup_page, "WAIT_SECONDS", 30), mock.patch("sys.stdin", Terminal(False)), contextlib.redirect_stdout(out):
            code = main(["init", "--host", posthog.url])
            keys = credentials.read()
        printed = out.getvalue()
        self.assertEqual(code, 0, printed)
        self.assertEqual((seen["foreign"], seen["guessed"]), (403, 404))
        self.assertIn(b"Orion setup", seen["page"])
        self.assertEqual(seen["wrong"]["ok"], False)
        self.assertIn("OpenAI did not accept the key", seen["wrong"]["text"])
        self.assertEqual(seen["right"], {"ok": True, "text": "OpenAI accepted the key and gpt-5.6-sol answered"})
        self.assertEqual(json.loads(self.config.read_text()), {"posthog_host": posthog.url, "posthog_project": "9", "llm_model": "gpt-5.6-sol"})
        self.assertEqual(keys, {"OPENAI_API_KEY": "sk-good"})
        self.assertIn("Opened Orion's setup page in your browser: http://127.0.0.1:", printed)
        self.assertIn("PostHog: ok (Signed in: Orion can query and pull project staging (9).)", printed)
        self.assertIn("AI labeller: ok", printed)
        for secret in ("sk-good", "sk-wrong", "pha_", "phr_"):
            self.assertNotIn(secret, printed)

    def test_the_page_moves_a_google_cloud_key_given_for_gemini_to_vertex_ai(self):
        google = FakeAPI(google_key("AQ.cloud", "vertex", "gemini-3.8-flash"))
        self.addCleanup(google.close)
        args = argparse.Namespace(host="http://127.0.0.1:9", project=None, llm=None, ui_host=None)
        with isolated_env(self.root, ORION_HOME=str(self.root / ".orion"), ORION_GEMINI_URL=google.url, ORION_VERTEX_URL=google.url):
            page = setup_page.Setup(Workspace(), args, 0)
            page.ai("gemini", "", "AQ.cloud")
            keys = credentials.read()
        self.assertEqual(page.notes["ai"]["ok"], True)
        self.assertIn("Vertex AI accepted it and gemini-3.8-flash answered", page.notes["ai"]["text"])
        self.assertEqual((page.model, json.loads(self.config.read_text())["llm_model"]), ("vertex:gemini-3.8-flash", "vertex:gemini-3.8-flash"))
        self.assertEqual(keys, {"VERTEX_API_KEY": "AQ.cloud"})


class FolderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "home"
        (self.home / "app" / ".orion").mkdir(parents=True)
        (self.home / "app" / "src").mkdir()
        (self.home / "other").mkdir()

    def test_the_home_folder_is_refused_and_a_project_is_found_from_below(self):
        with isolated_env(self.home.parent, HOME=str(self.home)):
            with mock.patch("pathlib.Path.cwd", return_value=self.home):
                out = io.StringIO()
                with contextlib.redirect_stderr(out), contextlib.redirect_stdout(out):
                    self.assertEqual(main(["init", "--no-input"]), 1)
                    self.assertEqual(main(["pull"]), 1)
                self.assertIn("is your home folder, not an app's folder", out.getvalue())
                self.assertFalse((self.home / ".orion").exists())
            (self.home / ".orion").mkdir()  # A stray ~/.orion from an earlier run never counts.
            with mock.patch("pathlib.Path.cwd", return_value=self.home / "app" / "src"):
                self.assertEqual(Workspace().root, self.home / "app" / ".orion")
                self.assertIsNone(Workspace().misplaced())
            with mock.patch("pathlib.Path.cwd", return_value=self.home / "other"):
                self.assertEqual(Workspace().root, self.home / "other" / ".orion")


class CheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def check(self, *argv, **env):
        out = io.StringIO()
        with isolated_env(self.root, ORION_HOME=str(self.root / ".orion"), **env), contextlib.redirect_stdout(out):
            code = main(["check", *argv])
        return code, out.getvalue()

    def test_check_says_what_works_and_what_fixes_the_rest(self):
        posthog = FakeOAuthPostHog(keys={"phx_short": [["query:read", "project:read"]]})
        openai = FakeAPI({("POST", "/v1/responses"): [keyed("authorization", "sk-good", {"output": []}, value=bearer)]})
        self.addCleanup(posthog.close)
        self.addCleanup(openai.close)
        Workspace(self.root / ".orion").save_config(posthog_host=posthog.url, posthog_project="9", llm_model="gpt-5.6-sol")
        with isolated_env(self.root):
            credentials.save({"POSTHOG_PERSONAL_API_KEY": "phx_short", "OPENAI_API_KEY": "sk-good"})
        code, printed = self.check(ORION_OPENAI_URL=openai.url)
        self.assertEqual(code, 1, printed)
        self.assertIn("✗ PostHog", printed)
        self.assertIn("lacks batch_export:read, batch_export:write, so `orion pull` cannot run", printed)
        self.assertIn("Fix: run `orion init` and sign in with PostHog", printed)
        self.assertIn("✓ AI labeller  gpt-5.6-sol: OpenAI accepted the key and gpt-5.6-sol answered", printed)
        self.assertIn("- TypeSafe", printed)
        with isolated_env(self.root):
            posthog_auth.sign_in(posthog.url, open_browser=browser, timeout=10, log=lambda *a: None)
        code, printed = self.check("--json", ORION_OPENAI_URL=openai.url)
        report = json.loads(printed)
        self.assertEqual(code, 0, printed)
        self.assertTrue(report["ok"])
        self.assertEqual([(c["name"], c["ok"]) for c in report["checks"]],
                         [("Folder", True), ("PostHog", True), ("AI labeller", True), ("TypeSafe", None)])
        self.assertIn("signed in; project staging (9)", report["checks"][1]["detail"])
        for secret in ("phx_short", "sk-good", "pha_"):
            self.assertNotIn(secret, printed)


if __name__ == "__main__":
    unittest.main()
