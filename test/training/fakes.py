"""Test stand-ins: a PostHog server that follows the documented export and query flows, and a scripted model."""
import gzip
import io
import json
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def _time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class FakePostHog:
    """Serves file download exports (create → poll → 302 to a signed URL) and the query API by query name."""

    def __init__(self, rows, project="1", key="phx_test"):
        self.rows, self.project, self.key = rows, project, key
        self.labels = []  # orion_label events, for context and search queries.
        self.runs, self.requests, self.signed_with_auth = {}, [], 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body=None, headers=None, raw=None):
                data = raw if raw is not None else json.dumps(body).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorised(self):
                return self.headers.get("Authorization") == f"Bearer {fake.key}"

            def do_GET(self):
                path = urlparse(self.path).path
                fake.requests.append(("GET", path))
                if path.startswith("/signed/"):
                    if self.headers.get("Authorization"):
                        fake.signed_with_auth += 1
                        return self.reply(400, {"error": "signed URLs must not carry the API key"})
                    run, index = path.rsplit("/", 1)[1].split("-")
                    return self.reply(200, raw=fake.runs[run]["files"][int(index)])
                if not self.authorised():
                    return self.reply(401, {"detail": "auth"})
                m = re.fullmatch(rf"/api/projects/{fake.project}/file_download_batch_exports/(\w+)/", path)
                if m:
                    run = fake.runs[m.group(1)]
                    run["polls"] += 1
                    if run["polls"] < 2:
                        return self.reply(200, {"id": m.group(1), "status": "Running"})
                    return self.reply(200, {"id": m.group(1), "status": "Completed", "files": [{"id": f"f{i}"} for i in range(len(run["files"]))]})
                m = re.fullmatch(rf"/api/projects/{fake.project}/file_download_batch_exports/(\w+)/download/(\d+)/", path)
                if m:
                    return self.reply(302, raw=b"", headers={"Location": f"/signed/{m.group(1)}-{m.group(2)}?sig=abc"})
                self.reply(404, {"detail": "not found"})

            def do_POST(self):
                path = urlparse(self.path).path
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                fake.requests.append(("POST", path, body))
                if not self.authorised():
                    return self.reply(401, {"detail": "auth"})
                if path == f"/api/projects/{fake.project}/file_download_batch_exports/":
                    return self.reply(202, {"id": fake.create_run(body)})
                if path == f"/api/projects/{fake.project}/query/":
                    return self.reply(200, fake.answer(body))
                self.reply(404, {"detail": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def create_run(self, body):
        assert body["model"] == "events" and body["file"]["format"] == "JSONLines"
        start, end = _time(body["data_interval_start"]), _time(body["data_interval_end"])
        assert (end - start).days <= 7, "PostHog allows at most one week per export"
        include = set(body.get("include") or [])
        chosen = [r for r in self.rows if start <= _time(r["timestamp"]) < end and (not include or r["event"] in include)]
        files = []
        for i in range(0, max(1, len(chosen)), 5000):  # Several files, like max_size_mb splitting.
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as f:
                for r in chosen[i:i + 5000]:
                    f.write((json.dumps(r) + "\n").encode())
            files.append(buf.getvalue())
        run = f"run{len(self.runs)}"
        self.runs[run] = {"files": files, "polls": 0}
        return run

    def answer(self, body):
        name, values = body.get("name"), body["query"].get("values", {})
        if name == "orion context":
            rows = [l for l in self.labels if l["properties"]["$session_id"] == values["session"]]
            rows.sort(key=lambda l: l["timestamp"], reverse=True)
            cols = ["timestamp", "model", "version", "target", "label", "confidence", "evidence", "since", "page", "agents"]
            return {"columns": cols, "results": [[l["timestamp"], l["properties"]["model"], l["properties"]["model_version"], l["properties"]["target"],
                                                  l["properties"]["label"], l["properties"]["confidence"], json.dumps(l["properties"]["evidence"]),
                                                  l["properties"]["since"], l["properties"].get("$pathname"), json.dumps(l["properties"].get("agents", []))]
                                                 for l in rows]}
        matched = [l for l in self.labels if ("label" not in values or str(l["properties"]["label"]) == values["label"])
                   and ("page" not in values or str(l["properties"].get("$pathname", "")).startswith(values["page"].rstrip("%")))]
        if name == "orion search sessions":
            sessions = {}
            for l in matched:
                s = sessions.setdefault(l["properties"]["$session_id"], {"session_id": l["properties"]["$session_id"], "first_seen": l["timestamp"],
                                                                          "last_seen": l["timestamp"], "labels": 0, "pages": [], "max_confidence": 0})
                s["labels"] += 1
                s["first_seen"] = min(s["first_seen"], l["timestamp"]); s["last_seen"] = max(s["last_seen"], l["timestamp"])
                if l["properties"].get("$pathname") not in s["pages"]:
                    s["pages"].append(l["properties"].get("$pathname"))
                s["max_confidence"] = max(s["max_confidence"], l["properties"]["confidence"])
            cols = ["session_id", "first_seen", "last_seen", "labels", "pages", "max_confidence"]
            return {"columns": cols, "results": [[s[c] for c in cols] for s in sessions.values()]}
        if name in ("orion search pages", "orion search totals"):
            source = matched if name == "orion search pages" else [l for l in self.labels if "page" not in values or
                                                                     str(l["properties"].get("$pathname", "")).startswith(values["page"].rstrip("%"))]
            pages = {}
            for l in source:
                pages.setdefault(l["properties"].get("$pathname"), set()).add(l["properties"]["$session_id"])
            return {"columns": ["page", "sessions"], "results": [[p, len(s)] for p, s in pages.items()]}
        raise AssertionError(f"Unexpected query {name}")


class ScriptedModel:
    """Answers the labellers' prompts from the evidence they contain, like a careful rater would."""
    model = "scripted-rater"

    def __init__(self):
        self.calls = 0

    def complete(self, system, prompt):
        self.calls += 1
        target = re.search(r'"(\w+)": <value or null>', prompt).group(1)
        if "Messages from one session" in prompt:
            out = []
            for i, text in re.findall(r"^\[(\d+)\] \+\d+s: (.*)$", prompt, re.M):
                angry = re.search(r"isn't working|won't|again|five times|didn't change|broken", text)
                out.append({"message": int(i), target: "frustrated" if angry else "calm", "confidence": 0.9, "reason": "wording"})
            return json.dumps(out)
        rages = [int(m) * 60 + float(s) for m, s in re.findall(r"^(\d+):(\d+\.\d) RAGE CLICK", prompt, re.M)]
        out = []
        for i, a, b in re.findall(r"^\[(\d+)\] ([\d.]+)s to ([\d.]+)s$", prompt, re.M):
            hit = any(float(a) - 5 <= r <= float(b) + 5 for r in rages)
            out.append({"window": int(i), target: "frustrated" if hit else "calm", "confidence": 0.7, "reason": "timeline"})
        return "Here are the labels:\n```json\n" + json.dumps(out) + "\n```"
