"""The language model behind Orion's AI labellers: Anthropic, OpenAI, Gemini or Gemini on Vertex AI (Google Cloud),
called with the customer's own key.
Every answer is cached in .orion/cache/llm.jsonl so rebuilding a dataset does not pay for the same labels twice.

A model name picks its provider (claude-* is Anthropic, gpt-* and o3-style names are OpenAI, gemini-* is Gemini),
or a prefix says it outright, e.g. openai:my-model; Vertex AI always takes its prefix, e.g. vertex:gemini-3.8-flash.
Keys come from credentials.get. ORION_ANTHROPIC_URL, ORION_OPENAI_URL, ORION_GEMINI_URL and ORION_VERTEX_URL point
the clients elsewhere (scheme and host, no /v1).
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import credentials
from .jev import MODEL as JEV_MODEL, URL as JEV_URL

PROVIDERS = ("anthropic", "openai", "gemini", "vertex")  # Also the order make_client looks for a saved key in.
NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Gemini", "vertex": "Vertex AI"}
KEY_NAMES = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "vertex": "VERTEX_API_KEY"}
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-5.6-sol", "gemini": "gemini-3.8-flash",
                  "vertex": "vertex:gemini-3.8-flash"}
BASE_URLS = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com",
             "gemini": "https://generativelanguage.googleapis.com", "vertex": "https://aiplatform.googleapis.com"}
KEY_PAGES = {"anthropic": "https://console.anthropic.com/settings/keys", "openai": "https://platform.openai.com/api-keys",
             "gemini": "https://aistudio.google.com/apikey", "vertex": "https://console.cloud.google.com/apis/credentials"}
BILLING_PAGES = {"anthropic": "https://console.anthropic.com/settings/billing",
                 "openai": "https://platform.openai.com/settings/organization/billing", "gemini": "https://console.cloud.google.com/billing",
                 "vertex": "https://console.cloud.google.com/billing"}
API_PAGES = {"gemini": "https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com",
             "vertex": "https://console.cloud.google.com/apis/library/aiplatform.googleapis.com"}  # Google turns APIs on per project.
RETRY = (429, 500, 502, 503, 529)  # Busy or briefly down: wait and ask again.
SETUP = (401, 402, 403, 404)  # Key, billing or model name: asking again cannot help. A 400 fails only that request.
TYPESAFE_CHECK = {"model": JEV_MODEL, "state": "Orion key check", "questions": {"ok": {
    "type": "noul", "instructions": "Is this a key check?", "criteria": {"true": "yes", "false": "no"}}}}


class LLMError(RuntimeError):
    def __init__(self, message, status=None, detail=""):
        super().__init__(message)
        self.status = status  # The HTTP status, when the API answered with one.
        self.detail = detail  # The whole reply, key blanked out, for explain(); the message keeps a short piece.


class LLMSetupError(LLMError):
    """Missing or rejected key, billing or a bad model name: retrying other sessions cannot help."""


def parse_json(text):
    """The first JSON object or array in a model reply (models sometimes wrap it in prose or fences)."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise LLMError("The model did not return JSON")
    return json.JSONDecoder().raw_decode(text[min(starts):])[0]


def base_url(service, base=None):
    return (base or os.environ.get(f"ORION_{service.upper()}_URL") or BASE_URLS[service]).rstrip("/")


def wait_seconds(retry_after, attempt):
    """Retry-After when the server gives seconds, else 2, 4, 8... seconds, at most a minute."""
    try:
        return min(600.0, max(0.0, float(retry_after)))
    except (TypeError, ValueError):
        return min(60, 2 ** attempt * 2)


def excerpt(raw, key=None, limit=300):
    """A one-line piece of an error reply (all of it with limit=None), with the key blanked out should the server echo it."""
    text = " ".join(raw.decode(errors="replace").split())
    return (text.replace(key, "[key]") if key else text)[:limit]


def send(name, method, url, headers, body=None, key=None, sleep=time.sleep, attempts=6):
    """One request under the shared retry policy; returns the reply body. Error messages never hold the key."""
    data = None if body is None else json.dumps(body).encode()
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, method=method, headers={**headers, "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code in RETRY and attempt < attempts - 1:
                sleep(wait_seconds(error.headers.get("retry-after"), attempt))
                continue
            kind = LLMSetupError if error.code in SETUP else LLMError
            raw = error.read()
            raise kind(f"{name} API returned HTTP {error.code}: {excerpt(raw, key)}", error.code, excerpt(raw, key, None)) from None
        except OSError as error:  # No connection, DNS, timeouts.
            host = urllib.parse.urlsplit(url).netloc
            raise LLMError(f"Could not reach the {name} API at {host}: {getattr(error, 'reason', error)}") from None
    raise LLMError(f"{name} API kept failing")


class Provider:
    """What the providers share: the key, the base URL and the retry policy. Each subclass says how to ask."""
    provider = models_path = ""
    max_tokens = 4096

    def __init__(self, model, api_key=None, max_tokens=None, sleep=time.sleep, base=None):
        self.model = model
        self.api_key = api_key or credentials.get(KEY_NAMES[self.provider])
        self.max_tokens = max_tokens or self.max_tokens
        self.sleep, self.base = sleep, base_url(self.provider, base)

    @property
    def name(self):
        return NAMES[self.provider]

    def complete(self, system, prompt):
        if not self.api_key:
            raise LLMSetupError(f"No {self.name} key: run `orion init` or set {KEY_NAMES[self.provider]}")
        path, body = self.request(system, prompt)
        return self.text(self.call("POST", path, body))

    def call(self, method, path, body=None, attempts=6):
        reply = send(self.name, method, self.base + path, self.headers(), body, self.api_key, self.sleep, attempts)
        try:
            return json.loads(reply) if reply else {}
        except ValueError:
            raise LLMError(f"{self.name} API replied with something other than JSON") from None

    def check(self):
        """Lists the provider's models, which only works with a valid key."""
        return self.call("GET", self.models_path, attempts=2)

    def ping(self):
        """The smallest real request (a client made with max_tokens=ping_tokens): the key, the account's credit and
        the model all have to work for it to succeed."""
        path, body = self.request("Answer in one word.", "Say OK.")
        return self.call("POST", path, body, attempts=2)


class Anthropic(Provider):
    provider, models_path, ping_tokens = "anthropic", "/v1/models", 1

    def headers(self):
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    def request(self, system, prompt):
        return "/v1/messages", {"model": self.model, "max_tokens": self.max_tokens, "temperature": 0, "system": system,
                                "messages": [{"role": "user", "content": prompt}]}

    @staticmethod
    def text(reply):
        return "".join(part.get("text", "") for part in reply.get("content") or [] if part.get("type") == "text")


class OpenAI(Provider):
    """The Responses API. No temperature: reasoning models reject it. store=false keeps prompts off OpenAI's servers."""
    provider, models_path, ping_tokens = "openai", "/v1/models", 16  # The Responses API's smallest allowance.

    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def request(self, system, prompt):
        return "/v1/responses", {"model": self.model, "instructions": system, "input": prompt,
                                 "max_output_tokens": self.max_tokens, "store": False}

    @staticmethod
    def text(reply):
        text = "".join(part.get("text", "") for item in reply.get("output") or [] for part in item.get("content") or []
                       if part.get("type") == "output_text")
        if not text and reply.get("status") == "incomplete":
            reason = (reply.get("incomplete_details") or {}).get("reason", "unknown")
            raise LLMError(f"OpenAI stopped before answering ({reason})")
        return text


class Gemini(Provider):
    provider, models_path, max_tokens, ping_tokens = "gemini", "/v1beta/models", 8192, 64  # Room for a thinking model.

    def headers(self):
        return {"x-goog-api-key": self.api_key}

    def request(self, system, prompt):
        return f"{self.models_path}/{urllib.parse.quote(self.model, safe='')}:generateContent", {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": self.max_tokens}}

    @staticmethod
    def text(reply):
        candidates = reply.get("candidates") or []
        if not candidates:
            raise LLMError(f"Gemini gave no answer ({(reply.get('promptFeedback') or {}).get('blockReason', 'no candidates')})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(part.get("text", "") for part in parts if not part.get("thought"))


class Vertex(Gemini):
    """Gemini on Vertex AI with a Google Cloud API key (usage billed to that Cloud project): the Gemini API's requests
    at another address, with no project or region in it."""
    provider, models_path = "vertex", "/v1beta1/publishers/google/models"


CLIENTS = {"anthropic": Anthropic, "openai": OpenAI, "gemini": Gemini, "vertex": Vertex}


def provider_for(model):
    """The provider for a model name: an 'anthropic:', 'openai:', 'gemini:' or 'vertex:' prefix wins, then the name's
    family (a bare gemini-* name is the Gemini API)."""
    name = str(model or "").strip()
    prefix, sep, _ = name.partition(":")
    if sep and prefix.lower() in PROVIDERS:
        return prefix.lower()
    lower = name.lower()
    if lower.startswith("claude-"):
        return "anthropic"
    if lower.startswith(("gpt-", "chatgpt-")) or re.match(r"o\d", lower):
        return "openai"
    if lower.startswith("gemini-"):
        return "gemini"
    raise LLMSetupError(f"Cannot tell which provider serves the model {name!r}: use a claude-*, gpt-*, o3-style or "
                        "gemini-* name, or put anthropic:, openai:, gemini: or vertex: in front of it")


def split_model(model):
    """(provider, the name its API expects): 'openai:gpt-5.6-sol' and 'gpt-5.6-sol' both give ('openai', 'gpt-5.6-sol')."""
    provider = provider_for(model)
    prefix, sep, rest = str(model).strip().partition(":")
    name = rest.strip() if sep and prefix.lower() == provider else str(model).strip()
    if not name:
        raise LLMSetupError(f"No model name after {prefix}:")
    return provider, name


def client_for(model, api_key=None, sleep=time.sleep, base=None):
    provider, name = split_model(model)
    return CLIENTS[provider](name, api_key, sleep=sleep, base=base)


def default_model():
    """The default model of the first provider with a key available: Anthropic, OpenAI, Gemini, then Vertex AI."""
    for provider in PROVIDERS:
        if credentials.get(KEY_NAMES[provider]):
            return DEFAULT_MODELS[provider]
    raise LLMSetupError("Run `orion init` to choose an AI labeller and save its key")


def failed(name, error):
    if error.status in SETUP:
        return f"{name} did not accept the key ({error})"
    return f"Could not check the {name} key ({error})"


def verify(provider, key, model=None, base=None, sleep=time.sleep):
    """Checks a key with one tiny request to the model it will label with, so a wrong key, an account without credit
    or a model the key cannot use shows up during setup rather than halfway through labelling.
    Returns (ok, message); the message never holds the key and says how to fix what failed."""
    if provider not in CLIENTS:
        raise ValueError(f"Unknown provider {provider!r}; use {', '.join(PROVIDERS)}")
    if not key:
        return False, f"No {NAMES[provider]} key given. Create one at {KEY_PAGES[provider]}"
    model = model or DEFAULT_MODELS[provider]
    client = CLIENTS[provider](model, key, max_tokens=CLIENTS[provider].ping_tokens, sleep=sleep, base=base)
    try:
        client.ping()
    except LLMError as error:
        return explain(provider, model, error)
    return True, f"{NAMES[provider]} accepted the key and {model} answered"


def explain(provider, model, error):
    """(ok, message) for a failed check, in words, with the page that fixes it. ok is None when the provider was
    only busy: the key may be fine, it just could not be checked now."""
    name, text = NAMES[provider], f"{error} {error.detail}".lower()
    if provider in API_PAGES and "service_disabled" in text:
        return False, f"The key's Google Cloud project has the {name} API turned off. Turn it on at {API_PAGES[provider]}, then check again"
    if error.status in (401, 403) or any(w in text for w in ("api key not valid", "api_key_invalid", "invalid x-api-key", "incorrect api key")):
        return False, f"{name} did not accept the key ({error.status or 'no answer'}). Create one at {KEY_PAGES[provider]}"
    if "free_tier" in text or "freetier" in text:
        return False, (f"The key is on {name}'s free tier, whose few requests a day for {model} are used up. Labelling needs "
                       f"many more: turn on billing for the key's Google Cloud project at {BILLING_PAGES[provider]}")
    if error.status == 402 or any(w in text for w in ("credit balance", "insufficient_quota", "billing", "quota", "resource_exhausted")):
        return False, f"{name} accepted the key, but its account cannot pay for requests. Add credit at {BILLING_PAGES[provider]}"
    if error.status == 404 or ("model" in text and any(w in text for w in ("not found", "does not exist", "not_found"))):
        return False, (f"{name} accepted the key, but the model {model} is not available to it. "
                       f"Choose {DEFAULT_MODELS[provider]} or another model the key can use")
    if error.status in RETRY:
        return None, f"{name} is busy right now (HTTP {error.status}), so the key could not be checked; try again in a minute"
    return False, f"Could not check the {name} key ({error})"


def verify_typesafe(key, base=None, sleep=time.sleep):
    """Checks a TypeSafe key with a one-question call to Jev. Returns (ok, message) like verify."""
    if not key:
        return False, "No TypeSafe key given"
    # ORION_TYPESAFE_URL is the whole endpoint, as in jev.py; base is scheme and host only.
    url = f"{base.rstrip('/')}/v1/systemone" if base else os.environ.get("ORION_TYPESAFE_URL") or JEV_URL
    try:
        send("TypeSafe", "POST", url, {"Authorization": f"Bearer {key}"}, TYPESAFE_CHECK, key, sleep, attempts=2)
    except LLMError as error:
        return False, failed("TypeSafe", error)
    return True, "TypeSafe accepted the key"


class Cached:
    """Wraps any client with .model and .complete(system, prompt); identical requests are answered from disk."""

    def __init__(self, client, path):
        self.client, self.path = client, path
        self.model = client.model
        self.memory = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.memory[row["key"]] = row["reply"]
        self.calls = 0

    def complete(self, system, prompt):
        key = hashlib.sha256(json.dumps([self.client.model, system, prompt]).encode()).hexdigest()
        if key in self.memory:
            return self.memory[key]
        reply = self.client.complete(system, prompt)
        self.calls += 1
        self.memory[key] = reply
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps({"key": key, "model": self.client.model, "reply": reply}) + "\n")
        return reply


def make_client(model, workspace, client=None):
    """A cached client. The model is the recipe's, else the workspace's llm_model, else the default model of the
    first provider with a key. A given client (tests, custom models) is wrapped as it is."""
    if client is None:
        client = client_for(model or workspace.config().get("llm_model") or default_model())
    return Cached(client, workspace.path("cache", "llm.jsonl"))
