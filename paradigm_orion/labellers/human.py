"""Human labelling page: `orion label human <recipe>` serves a local page (127.0.0.1 only) that shows each
window's input as a strip, links to the PostHog replay at that moment, and saves answers to
.orion/labels/<recipe>/human.jsonl. Windows the AI labellers already rated come first, so people review them."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..dataset.pipeline import decode_records
from ..labels import coerce, label_path, load_all, make_label
from ..posthog import epoch_ms
from ..store import recorders_by_session
from ..workspace import read_jsonl, write_jsonl
from .replay import session_windows

PAGE = Path(__file__).with_name("human.html")
VERSION = "human-v1"


def strip(records, start, end):
    """Downsampled input for drawing: pointer speed per 100 ms, key holds, clicks, scrolls, focus and gaps."""
    bins = int((end - start) // 100) + 1
    speed, last = [0.0] * bins, {}
    keys, clicks, scrolls, focus, gaps, held = [], [], [], [], [], {}
    for r in sorted(records, key=lambda r: r["t"]):
        t, kind = r["t"], r["kind"]
        if not start <= t < end:
            continue
        x = t - start
        if kind == "pointermove" and "x" in r and "y" in r:
            p = last.get(r.get("pointer_id", 0))
            if p and 0 < t - p[0] < 100:
                v = ((r["x"] - p[1]) ** 2 + (r["y"] - p[2]) ** 2) ** 0.5 * 1000 / (t - p[0])
                i = int(x // 100)
                speed[i] = max(speed[i], v)
            last[r.get("pointer_id", 0)] = (t, r["x"], r["y"])
        elif kind == "keydown" and not r.get("repeat"):
            held[r.get("press_id", r.get("code"))] = x
            keys.append([x, None])
        elif kind == "keyup":
            down = held.pop(r.get("press_id", r.get("code")), None)
            if down is not None:
                for k in reversed(keys):
                    if k[0] == down and k[1] is None:
                        k[1] = x
                        break
        elif kind == "pointerdown":
            clicks.append(x)
        elif kind == "wheel":
            scrolls.append(x)
        elif kind in ("focusin", "focusout"):
            focus.append([x, kind == "focusin"])
        elif kind == "boundary":
            gaps.append([x, r.get("reason")])
    return {"length_ms": end - start, "speed": [round(v) for v in speed], "keys": keys, "clicks": clicks, "scrolls": scrolls, "focus": focus, "gaps": gaps}


class Session:
    def __init__(self, store, recipe, client=None, limit=200, show_ai=False):
        self.store, self.recipe, self.client, self.show_ai = store, recipe, client, show_ai
        self.path = label_path(store.ws, recipe, "human")
        self.lock = threading.Lock()
        by_session = recorders_by_session(store.recordings())
        existing = load_all(store.ws, recipe)
        ai = {}
        for name in ("text", "replay"):
            for l in existing[name]:
                ai.setdefault(l["recorder_id"], []).append(l)
        session_start = {}
        for row in store.iter_context(events={"$pageview"}):
            if row.get("session_id"):
                t = epoch_ms(row["timestamp"])
                session_start[row["session_id"]] = min(session_start.get(row["session_id"], t), t)
        items = []
        for session_id, recs in by_session.items():
            for w in session_windows(recs, recipe["window"]["length_ms"], recipe["window"]["stride_ms"]):
                rid = w["recorder"]["recorder_id"]
                overlapping = [l for l in ai.get(rid, []) if l["start_ms"] < w["end_ms"] and w["start_ms"] < l["end_ms"]]
                key = hashlib.sha256(f"{rid}:{w['start_ms']}".encode()).hexdigest()
                items.append({"id": key[:16], "recorder_id": rid, "session_id": session_id, "start_ms": w["start_ms"], "end_ms": w["end_ms"],
                              "epoch_start": w["epoch_start"], "offset_ms": w["epoch_start"] - session_start.get(session_id, w["epoch_start"]),
                              "ai": [{"labeller": l["labeller"], "value": l["targets"][recipe["target"]["name"]], "confidence": l["confidence"],
                                      "reason": l.get("reason", "")} for l in overlapping], "order": (0 if overlapping else 1, key)})
        items.sort(key=lambda i: i["order"])
        self.items = items[:limit]
        wanted = {i["recorder_id"] for i in self.items}
        self.records = {}
        for row in store.iter_telemetry():
            p = row["properties"]
            if p.get("recorder_id") in wanted:
                try:
                    self.records.setdefault(p["recorder_id"], []).extend(decode_records(p))
                except ValueError:
                    continue
        self.answers = {l["window_id"]: l for l in read_jsonl(self.path) if l.get("window_id")}

    def state(self):
        t = self.recipe["target"]
        return {"recipe": self.recipe["name"], "target": {k: t.get(k) for k in ("name", "type", "classes", "min", "max", "binary")},
                "total": len(self.items), "done": sum(i["id"] in self.answers for i in self.items), "show_ai": self.show_ai,
                "items": [{"id": i["id"], "answered": i["id"] in self.answers} for i in self.items]}

    def item(self, index):
        i = self.items[index]
        replay = self.client.replay_url(i["session_id"], i["offset_ms"]) if self.client and self.client.project_id else None
        answer = self.answers.get(i["id"])
        return {"index": index, "id": i["id"], "session_id": i["session_id"], "offset_ms": i["offset_ms"], "replay_url": replay,
                "strip": strip(self.records.get(i["recorder_id"], []), i["start_ms"], i["end_ms"]),
                "ai": i["ai"] if self.show_ai else [], "answer": answer["targets"][self.recipe["target"]["name"]] if answer else None,
                "confidence": answer["confidence"] if answer else None}

    def answer(self, window_id, value, confidence):
        item = next((i for i in self.items if i["id"] == window_id), None)
        if item is None:
            raise ValueError("Unknown window")
        with self.lock:
            if value is None:
                self.answers.pop(window_id, None)
            else:
                coerced = coerce(self.recipe["target"], value)
                if coerced is None:
                    raise ValueError("Answer is not a valid value for this target")
                self.answers[window_id] = make_label(self.recipe, "human", item["recorder_id"], item["start_ms"], item["end_ms"], coerced,
                                                     confidence, VERSION, source="human", session_id=item["session_id"], window_id=window_id)
            write_jsonl(self.path, sorted(self.answers.values(), key=lambda l: (l["recorder_id"], l["start_ms"])))


def serve(session, port=8795, open_browser=True, log=print):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def local(self):
            # Refuse other hostnames so a web page cannot reach this server through DNS rebinding.
            return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self.local():
                return self.send(403, {"error": "Forbidden"})
            if self.path == "/":
                return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self.send(200, session.state())
            if self.path.startswith("/api/item/"):
                try:
                    return self.send(200, session.item(int(self.path.rsplit("/", 1)[1])))
                except (ValueError, IndexError):
                    return self.send(404, {"error": "No such window"})
            self.send(404, {"error": "Not found"})

        def do_POST(self):
            if not self.local() or self.path != "/api/label" or self.headers.get("Content-Type") != "application/json":
                return self.send(404, {"error": "Not found"})
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 10000)))
                session.answer(body.get("id"), body.get("value"), float(body.get("confidence", 1.0)))
                return self.send(200, session.state())
            except (ValueError, TypeError) as error:
                return self.send(400, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    log(f"Labelling page: {url}  ({len(session.items)} windows; Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    return server, url
