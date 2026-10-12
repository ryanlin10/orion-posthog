"""`orion init` and where keys live: the keys file outside the project (environment first, mode 0600), the
interactive setup with checks against local stand-ins for PostHog, OpenAI and TypeSafe, and the no-questions mode."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_llm import FakeAPI, isolated_env, keyed
from paradigm_orion import credentials, onboarding
from paradigm_orion.cli import main
from paradigm_orion.posthog import PostHog


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

    def run_init(self, argv, asked=(), typed=(), tty=True, **env):
        """Runs `orion init` with scripted answers; returns (exit code, what it printed, the two scripts)."""
        ask, ask_secret = Script(*asked), Script(*typed)
        out, err = io.StringIO(), io.StringIO()
        with isolated_env(self.root, ORION_HOME=str(self.root / ".orion"), **env), \
                mock.patch.object(onboarding, "ask", ask), mock.patch.object(onboarding, "ask_secret", ask_secret), \
                mock.patch("sys.stdin", Terminal(tty)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["init", *argv])
        self.assertEqual((ask.answers, ask_secret.answers), ([], []), "every scripted answer is used")
        return code, out.getvalue() + err.getvalue(), ask, ask_secret

    def saved_keys(self):
        with isolated_env(self.root):
            return credentials.read()

    def test_first_setup_checks_each_key_and_saves_settings_and_keys_apart(self):
        projects = {"results": [{"id": 7, "name": "acme-web"}, {"id": 9, "name": "staging"}]}
        posthog = self.serve({("GET", "/api/projects/"): [keyed("authorization", "phx_good", projects, value=bearer)]})
        openai = self.serve({("GET", "/v1/models"): [keyed("authorization", "sk-good", value=bearer)]})
        typesafe = self.serve({("POST", "/v1/systemone"): [(401, {"error": "unknown key"})]})
        code, printed, ask, ask_secret = self.run_init(
            [], asked=[posthog.url, "", "staging", "openai", "", "k"], typed=["phx_wrong", "phx_good", "sk-good", "ts-unchecked"],
            ORION_OPENAI_URL=openai.url, ORION_TYPESAFE_URL=f"{typesafe.url}/v1/systemone")
        self.assertEqual(code, 0, printed)
        self.assertEqual(json.loads(self.config.read_text()), {"posthog_host": posthog.url, "posthog_project": "9", "llm_model": "gpt-5.6-sol"})
        self.assertEqual(self.saved_keys(), {"POSTHOG_PERSONAL_API_KEY": "phx_good", "OPENAI_API_KEY": "sk-good", "TYPESAFE_API_KEY": "ts-unchecked"})
        self.assertEqual(stat.S_IMODE(self.keys.stat().st_mode), 0o600)
        self.assertIn("PostHog did not accept the key", printed)
        self.assertIn("  7  acme-web\n  9  staging\n", printed)
        self.assertIn("OpenAI accepted the key", printed)
        self.assertIn("TypeSafe did not accept the key", printed)
        self.assertIn(f"to {self.keys} (readable only by you)", printed)
        self.assertIn(f"to {self.config}", printed)
        for key in ("phx_wrong", "phx_good", "sk-good", "ts-unchecked"):
            self.assertNotIn(key, printed)
            self.assertNotIn(key, self.config.read_text())
        self.assertTrue(all("hidden" in p for p in ask_secret.prompts))
        self.assertEqual(ask.prompts[0], "PostHog host [https://us.posthog.com]: ")
        self.assertEqual(ask.prompts[2], "PostHog project id: ")
        self.assertEqual(ask.prompts[4], "Model [gpt-5.6-sol]: ")
        self.assertEqual([r["headers"]["authorization"] for r in posthog.requests], ["Bearer phx_wrong", "Bearer phx_good"])

    def test_saved_keys_are_kept_with_enter(self):
        self.keys.parent.mkdir()
        self.keys.write_text("POSTHOG_PERSONAL_API_KEY=phx_saved\nANTHROPIC_API_KEY=sk-ant-saved\n")
        before = self.keys.read_bytes()
        code, printed, _, _ = self.run_init(["--project", "42", "--no-check"], asked=["http://127.0.0.1:9", "", ""], typed=["", "", ""])
        self.assertEqual(code, 0, printed)
        self.assertEqual(json.loads(self.config.read_text()), {"posthog_host": "http://127.0.0.1:9", "posthog_project": "42", "llm_model": "claude-sonnet-5-5"})
        self.assertEqual(self.keys.read_bytes(), before)
        self.assertIn(f"PostHog personal API key: one is already set ({self.keys}); press Enter to keep it.", printed)
        self.assertIn("Skipped the TypeSafe API key", printed)
        self.assertNotIn("readable only by you", printed)  # Nothing new to write to the keys file.

    def test_llm_flag_skips_the_questions_it_answers_and_notes_a_key_the_environment_overrides(self):
        code, printed, ask, _ = self.run_init(["--host", "http://127.0.0.1:9", "--project", "3", "--llm", "openai:gpt-5.6-mini", "--no-check"],
                                              typed=["phx_typed", "sk-typed", ""], OPENAI_API_KEY="sk-env")
        self.assertEqual(code, 0, printed)
        self.assertEqual(ask.prompts, [])
        self.assertEqual(json.loads(self.config.read_text())["llm_model"], "openai:gpt-5.6-mini")
        self.assertEqual(self.saved_keys(), {"POSTHOG_PERSONAL_API_KEY": "phx_typed", "OPENAI_API_KEY": "sk-typed"})
        self.assertIn("OPENAI_API_KEY is also set in your environment, and Orion uses that one first", printed)
        self.assertNotIn("sk-env", printed)

    def test_stopping_part_way_keeps_what_was_saved(self):
        code, printed, _, _ = self.run_init(["--host", "http://127.0.0.1:9", "--project", "3", "--no-check"],
                                            asked=[KeyboardInterrupt()], typed=["phx_typed"])
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
        self.assertNotIn("sk-env", printed)
        code, printed, _, _ = self.run_init(["--llm", "gemini:gemini-3.8-pro"], tty=False)
        self.assertEqual((code, json.loads(self.config.read_text())["llm_model"]), (0, "gemini:gemini-3.8-pro"))
        self.assertIn("Keys found: none.", printed)
        code, printed, _, _ = self.run_init(["--llm", "llama-3", "--no-input"])
        self.assertEqual(code, 1)
        self.assertIn("orion: Cannot tell which provider serves the model 'llama-3'", printed)


if __name__ == "__main__":
    unittest.main()
