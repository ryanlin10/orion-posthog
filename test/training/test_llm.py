"""The AI labellers' model clients against local stand-ins for the Anthropic, OpenAI, Gemini and TypeSafe APIs:
request shapes, text extraction, the retry policy, setup errors, key checks and which model make_client picks."""
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from fakes import ScriptedModel
from paradigm_orion import credentials
from paradigm_orion.llm import (BILLING_PAGES, KEY_PAGES, NAMES, TYPESAFE_CHECK, Anthropic, Cached, Gemini, LLMError, LLMSetupError,
                                OpenAI, make_client, provider_for, split_model, verify, verify_typesafe)
from paradigm_orion.workspace import Workspace


class FakeAPI:
    """A local server that answers from a script {(method, path): [answer, ...]}. An answer is (status, body) or
    (status, body, headers), or a function of the request that returns one; the last answer repeats."""

    def __init__(self, routes):
        self.routes = {route: list(answers) for route, answers in routes.items()}
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.answer(self, "GET")

            def do_POST(self):
                fake.answer(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def answer(self, handler, method):
        raw = handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
        request = {"method": method, "path": urlparse(handler.path).path, "body": json.loads(raw) if raw else None,
                   "headers": {k.lower(): v for k, v in handler.headers.items()}}
        self.requests.append(request)
        answers = self.routes.get((method, request["path"])) or [(404, {"error": "no such route"})]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        status, body, headers = (*(answer(request) if callable(answer) else answer), {})[:3]
        data = json.dumps(body).encode()
        handler.send_response(status)
        for k, v in headers.items():
            handler.send_header(k, v)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def keyed(header, good, body=None, value=lambda key: key):
    """An answer that is 200 only when the request carries the good key in the given header."""
    return lambda request: (200, body or {"data": []}) if request["headers"].get(header) == value(good) else (401, {"error": "invalid key"})


def isolated_env(root, **extra):
    """No real keys or settings: a cleared environment and a keys file in the test's own folder."""
    return mock.patch.dict(os.environ, {"ORION_KEYS_FILE": str(Path(root) / "keys" / "keys.env"), **extra}, clear=True)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(isolated_env(tmp.name))
        self.sleeps = []

    def serve(self, routes):
        fake = FakeAPI(routes)
        self.addCleanup(fake.close)
        return fake

    def test_anthropic_sends_the_messages_request_and_retries_when_busy(self):
        fake = self.serve({("POST", "/v1/messages"): [(429, {"error": "busy"}, {"Retry-After": "3"}),
                                                      (200, {"content": [{"type": "thinking", "thinking": "hmm"},
                                                                         {"type": "text", "text": "[1, "}, {"type": "text", "text": "2]"}]})]})
        client = Anthropic("claude-test", "sk-ant-test", sleep=self.sleeps.append, base=fake.url)
        self.assertEqual(client.complete("be careful", "label this"), "[1, 2]")
        self.assertEqual(self.sleeps, [3.0])
        sent = fake.requests[-1]
        self.assertEqual((sent["headers"]["x-api-key"], sent["headers"]["anthropic-version"]), ("sk-ant-test", "2023-06-01"))
        self.assertEqual(sent["body"], {"model": "claude-test", "max_tokens": 4096, "temperature": 0, "system": "be careful",
                                        "messages": [{"role": "user", "content": "label this"}]})

    def test_openai_uses_the_responses_api_without_temperature(self):
        reply = {"status": "completed", "output": [
            {"type": "reasoning", "content": None, "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": '{"a": '}, {"type": "refusal", "refusal": "no"},
                                            {"type": "output_text", "text": "1}"}]}]}
        fake = self.serve({("POST", "/v1/responses"): [(200, reply)]})
        client = OpenAI("gpt-test", "sk-test", sleep=self.sleeps.append, base=fake.url)
        self.assertEqual(client.complete("system words", "prompt words"), '{"a": 1}')
        sent = fake.requests[-1]
        self.assertEqual(sent["headers"]["authorization"], "Bearer sk-test")
        self.assertEqual(sent["body"], {"model": "gpt-test", "instructions": "system words", "input": "prompt words",
                                        "max_output_tokens": 4096, "store": False})

    def test_openai_says_when_it_stopped_before_answering(self):
        fake = self.serve({("POST", "/v1/responses"): [(200, {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                                                               "output": [{"type": "reasoning", "content": None}]})]})
        with self.assertRaisesRegex(LLMError, "max_output_tokens"):
            OpenAI("gpt-test", "sk-test", base=fake.url).complete("s", "p")

    def test_gemini_sends_generate_content_and_skips_thought_parts(self):
        reply = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Let me think...", "thought": True}, {"text": "[\"calm\"]"}]}}]}
        fake = self.serve({("POST", "/v1beta/models/gemini-test:generateContent"): [(503, {}), (200, reply)]})
        client = Gemini("gemini-test", "AIza-test", sleep=self.sleeps.append, base=fake.url)
        self.assertEqual(client.complete("sys", "prompt"), '["calm"]')
        self.assertEqual(self.sleeps, [2])
        sent = fake.requests[-1]
        self.assertEqual(sent["headers"]["x-goog-api-key"], "AIza-test")
        self.assertEqual(sent["body"], {"systemInstruction": {"parts": [{"text": "sys"}]},
                                        "contents": [{"role": "user", "parts": [{"text": "prompt"}]}],
                                        "generationConfig": {"temperature": 0, "maxOutputTokens": 8192}})

    def test_gemini_blocked_prompt_is_an_error(self):
        fake = self.serve({("POST", "/v1beta/models/gemini-test:generateContent"): [(200, {"promptFeedback": {"blockReason": "SAFETY"}})]})
        with self.assertRaisesRegex(LLMError, "SAFETY"):
            Gemini("gemini-test", "AIza-test", base=fake.url).complete("s", "p")

    def test_busy_errors_back_off_then_give_up_as_ordinary_errors(self):
        fake = self.serve({("POST", "/v1/responses"): [(500, {}), (502, {}), (529, {}), (503, {}), (429, {}), (500, {"error": "still down"})]})
        with self.assertRaises(LLMError) as caught:
            OpenAI("gpt-test", "sk-test", sleep=self.sleeps.append, base=fake.url).complete("s", "p")
        self.assertNotIsInstance(caught.exception, LLMSetupError)
        self.assertEqual(self.sleeps, [2, 4, 8, 16, 32])
        self.assertEqual(len(fake.requests), 6)
        self.assertEqual(caught.exception.status, 500)

    def test_key_billing_and_model_errors_stop_at_once_and_never_show_the_key(self):
        for status in (401, 402, 403, 404):
            fake = self.serve({("POST", "/v1/messages"): [(status, {"error": {"message": "bad key sk-ant-secret-123"}})]})
            with self.assertRaises(LLMSetupError) as caught:
                Anthropic("claude-test", "sk-ant-secret-123", sleep=self.sleeps.append, base=fake.url).complete("s", "p")
            message = str(caught.exception)
            self.assertIn(f"Anthropic API returned HTTP {status}", message)
            self.assertIn("bad key [key]", message)
            self.assertNotIn("sk-ant-secret-123", message)
            self.assertEqual(len(fake.requests), 1)
        # A 400 is one bad request (say, a prompt too long): that chunk fails, the labelling run goes on.
        fake = self.serve({("POST", "/v1/messages"): [(400, {"error": {"message": "prompt is too long"}})]})
        with self.assertRaises(LLMError) as one:
            Anthropic("claude-test", "sk-ant-secret-123", sleep=self.sleeps.append, base=fake.url).complete("s", "p")
        self.assertNotIsInstance(one.exception, LLMSetupError)
        fake = self.serve({("POST", "/v1/responses"): [(418, {"error": "teapot"})]})
        with self.assertRaises(LLMError) as other:
            OpenAI("gpt-test", "sk-test", sleep=self.sleeps.append, base=fake.url).complete("s", "p")
        self.assertNotIsInstance(other.exception, LLMSetupError)
        self.assertEqual(self.sleeps, [])

    def test_missing_key_and_unreachable_api(self):
        with self.assertRaisesRegex(LLMSetupError, r"orion init.*OPENAI_API_KEY"):
            OpenAI("gpt-test").complete("s", "p")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaisesRegex(LLMError, "Could not reach the Gemini API"):
            Gemini("gemini-test", "AIza-test", base=f"http://127.0.0.1:{port}").complete("s", "p")

    def test_base_urls_come_from_the_argument_then_the_environment(self):
        with mock.patch.dict(os.environ, {"ORION_OPENAI_URL": "http://openai.local/", "ORION_GEMINI_URL": "http://gemini.local"}):
            self.assertEqual(OpenAI("gpt-test", "k").base, "http://openai.local")
            self.assertEqual(OpenAI("gpt-test", "k", base="http://other.local").base, "http://other.local")
            self.assertEqual(Gemini("gemini-test", "k").base, "http://gemini.local")
        self.assertEqual(Anthropic("claude-test", "k").base, "https://api.anthropic.com")

    def test_verify_asks_the_chosen_model_for_one_tiny_answer_with_each_providers_header(self):
        checks = {"anthropic": ("claude-x", "/v1/messages", "x-api-key", lambda k: k, lambda b: b["max_tokens"], 1),
                  "openai": ("gpt-x", "/v1/responses", "authorization", lambda k: f"Bearer {k}", lambda b: b["max_output_tokens"], 16),
                  "gemini": ("gemini-x", "/v1beta/models/gemini-x:generateContent", "x-goog-api-key", lambda k: k,
                             lambda b: b["generationConfig"]["maxOutputTokens"], 64)}
        for provider, (model, path, header, value, budget, tokens) in checks.items():
            fake = self.serve({("POST", path): [keyed(header, "good-key", {"anything": "goes"}, value=value)]})
            self.assertEqual(verify(provider, "good-key", model, base=fake.url), (True, f"{NAMES[provider]} accepted the key and {model} answered"))
            self.assertEqual(budget(fake.requests[0]["body"]), tokens)
            ok, message = verify(provider, "wrong-key-xyz", model, base=fake.url, sleep=self.sleeps.append)
            self.assertFalse(ok)
            self.assertEqual(message, f"{NAMES[provider]} did not accept the key (401). Create one at {KEY_PAGES[provider]}")
            self.assertNotIn("wrong-key-xyz", message)
            if provider == "anthropic":
                self.assertEqual(fake.requests[0]["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(verify("openai", ""), (False, f"No OpenAI key given. Create one at {KEY_PAGES['openai']}"))

    def test_verify_says_in_words_what_is_wrong_and_where_to_fix_it(self):
        cases = [("anthropic", "/v1/messages", (400, {"error": {"message": "Your credit balance is too low to access the Anthropic API."}}),
                  f"Anthropic accepted the key, but its account cannot pay for requests. Add credit at {BILLING_PAGES['anthropic']}"),
                 ("openai", "/v1/responses", (429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}),
                  f"OpenAI accepted the key, but its account cannot pay for requests. Add credit at {BILLING_PAGES['openai']}"),
                 ("gemini", "/v1beta/models/gemini-x:generateContent", (400, {"error": {"message": "API key not valid. Please pass a valid API key."}}),
                  f"Gemini did not accept the key (400). Create one at {KEY_PAGES['gemini']}"),
                 ("anthropic", "/v1/messages", (404, {"error": {"message": "model: claude-x"}}),
                  "Anthropic accepted the key, but the model claude-x is not available to it. Choose claude-sonnet-5-5 or another model the key can use")]
        for provider, path, answer, expected in cases:
            fake = self.serve({("POST", path): [answer]})
            model = {"anthropic": "claude-x", "openai": "gpt-x", "gemini": "gemini-x"}[provider]
            self.assertEqual(verify(provider, "k-123456789012", model, base=fake.url, sleep=self.sleeps.append), (False, expected))

    def test_verify_typesafe_asks_jev_one_question(self):
        fake = self.serve({("POST", "/v1/systemone"): [keyed("authorization", "ts-good", {"answers": {"ok": True}}, value=lambda k: f"Bearer {k}")]})
        self.assertEqual(verify_typesafe("ts-good", base=fake.url), (True, "TypeSafe accepted the key"))
        self.assertEqual(fake.requests[0]["body"], TYPESAFE_CHECK)
        self.assertEqual(TYPESAFE_CHECK["model"], "jev-1.13.0")
        ok, message = verify_typesafe("ts-wrong", base=fake.url)
        self.assertFalse(ok)
        self.assertIn("TypeSafe did not accept the key", message)
        self.assertNotIn("ts-wrong", message)


class ModelChoiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(isolated_env(self.root))
        self.ws = Workspace(self.root / ".orion")

    def test_provider_for_reads_prefixes_then_families(self):
        cases = {"claude-sonnet-5-5": "anthropic", "gpt-5.6-sol": "openai", "chatgpt-4o-latest": "openai", "o3": "openai",
                 "o4-mini": "openai", "gemini-3.8-flash": "gemini", "openai:my-fine-tune": "openai", "Gemini:custom": "gemini",
                 "anthropic:gpt-lookalike": "anthropic"}
        for model, provider in cases.items():
            self.assertEqual(provider_for(model), provider, model)
        for bad in ("llama-3", "omni", "", None, "mistral:large"):
            with self.assertRaisesRegex(LLMSetupError, "anthropic:, openai: or gemini:"):
                provider_for(bad)
        self.assertEqual(split_model("openai:gpt-5.6-sol"), ("openai", "gpt-5.6-sol"))
        self.assertEqual(split_model("openai:ft:gpt-4o:acme"), ("openai", "ft:gpt-4o:acme"))
        with self.assertRaises(LLMSetupError):
            split_model("gemini:")

    def test_make_client_takes_the_recipe_then_the_saved_model_then_the_first_saved_key(self):
        with self.assertRaisesRegex(LLMSetupError, "Run `orion init` to choose an AI labeller and save its key"):
            make_client(None, self.ws)
        credentials.save({"GEMINI_API_KEY": "AIza-file"})
        client = make_client(None, self.ws)
        self.assertIsInstance(client, Cached)
        self.assertIsInstance(client.client, Gemini)
        self.assertEqual((client.model, client.client.api_key), ("gemini-3.8-flash", "AIza-file"))
        credentials.save({"OPENAI_API_KEY": "sk-file"})
        self.assertEqual(make_client(None, self.ws).model, "gpt-5.6-sol")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-env"}):
            client = make_client(None, self.ws)
            self.assertEqual((client.model, client.client.api_key), ("claude-sonnet-5-5", "sk-ant-env"))
            self.ws.save_config(llm_model="openai:gpt-5.6-sol")
            client = make_client(None, self.ws)
            self.assertEqual((type(client.client), client.model, client.client.api_key), (OpenAI, "gpt-5.6-sol", "sk-file"))
            client = make_client("gemini:gemini-3.8-pro", self.ws)
            self.assertEqual((type(client.client), client.model), (Gemini, "gemini-3.8-pro"))
        scripted = ScriptedModel()
        self.assertIs(make_client(None, self.ws, scripted).client, scripted)

    def test_answers_are_cached_per_model_and_prompt(self):
        fake = FakeAPI({("POST", "/v1/responses"): [(200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": "[]"}]}]})]})
        self.addCleanup(fake.close)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env", "ORION_OPENAI_URL": fake.url}):
            client = make_client("gpt-5.6-sol", self.ws)
            self.assertEqual([client.complete("s", "p"), client.complete("s", "p")], ["[]", "[]"])
            self.assertEqual(make_client("gpt-5.6-sol", self.ws).complete("s", "p"), "[]")
        self.assertEqual((client.calls, len(fake.requests)), (1, 1))
        self.assertNotIn("sk-env", self.ws.path("cache", "llm.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
