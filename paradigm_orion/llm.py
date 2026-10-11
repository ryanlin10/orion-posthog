"""The language model behind Orion's AI labellers. Calls run with the customer's own key, and every answer is
cached in .orion/cache/llm.jsonl so rebuilding a dataset does not pay for the same labels twice."""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class LLMError(RuntimeError):
    pass


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


class Anthropic:
    def __init__(self, model, api_key=None, max_tokens=4096, sleep=time.sleep):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.max_tokens, self.sleep = max_tokens, sleep

    def complete(self, system, prompt):
        if not self.api_key:
            raise LLMSetupError("Set ANTHROPIC_API_KEY to use the AI labellers")
        body = json.dumps({"model": self.model, "max_tokens": self.max_tokens, "temperature": 0, "system": system,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        for attempt in range(6):
            req = urllib.request.Request(ANTHROPIC_URL, data=body, method="POST", headers={
                "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=300) as response:
                    reply = json.loads(response.read())
                return "".join(part.get("text", "") for part in reply.get("content", []) if part.get("type") == "text")
            except urllib.error.HTTPError as error:
                if error.code in (429, 500, 502, 503, 529) and attempt < 5:
                    retry = error.headers.get("retry-after")
                    self.sleep(float(retry) if retry and retry.isdigit() else min(60, 2 ** attempt * 2))
                    continue
                detail = f"Anthropic API returned HTTP {error.code}: {error.read().decode(errors='replace')[:300]}"
                raise (LLMSetupError if error.code in (401, 402, 403, 404) else LLMError)(detail) from None
        raise LLMError("Anthropic API kept failing")


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
    return Cached(client or Anthropic(model), workspace.path("cache", "llm.jsonl"))
