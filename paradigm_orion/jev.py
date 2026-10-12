"""Jev, TypeSafe's decision model: answers multiple-choice questions about a described situation with a probability
for every option. Orion uses it to rank the self-report answers by relevance. Answers are cached in
.orion/cache/jev.jsonl, and the key comes from TYPESAFE_API_KEY or the keys file `orion init` writes."""
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"


class JevError(RuntimeError):
    pass


class Jev:
    def __init__(self, workspace, api_key=None, url=None, model=MODEL, sleep=time.sleep):
        from .credentials import get
        self.api_key = api_key or get("TYPESAFE_API_KEY")
        self.url = url or os.environ.get("ORION_TYPESAFE_URL") or URL
        self.model, self.sleep = model, sleep
        self.cache_path = workspace.path("cache", "jev.jsonl")
        self.memory = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.memory[row["key"]] = row["answers"]
        self.calls = 0
        self.lock = threading.Lock()

    def ask(self, state, questions):
        """questions: {name: {"type": "choice", "instructions": ..., "criteria": {option: description}}}.
        Returns {name: {"choice": ..., "probabilities": {option: p}, ...}}."""
        if not self.api_key:
            raise JevError("Save a TypeSafe key with `orion init` (or set TYPESAFE_API_KEY) to rank answers with Jev")
        body = {"model": self.model, "state": state, "questions": questions}
        key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if key in self.memory:
            return self.memory[key]
        data = json.dumps(body).encode()
        for attempt in range(6):
            req = urllib.request.Request(self.url, data=data, method="POST", headers={
                "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as response:
                    answers = json.loads(response.read())["answers"]
                break
            except urllib.error.HTTPError as error:
                if error.code in (429, 500, 502, 503, 529) and attempt < 5:
                    self.sleep(min(30, 2 ** attempt))
                    continue
                raise JevError(f"Jev returned HTTP {error.code}: {error.read().decode(errors='replace')[:300]}") from None
            except urllib.error.URLError as error:
                if attempt < 5:
                    self.sleep(min(30, 2 ** attempt))
                    continue
                raise JevError(f"Could not reach Jev: {error.reason}") from None
        with self.lock:  # `orion survey` asks from several threads.
            self.calls += 1
            self.memory[key] = answers
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("a") as f:
                f.write(json.dumps({"key": key, "answers": answers}) + "\n")
        return answers
