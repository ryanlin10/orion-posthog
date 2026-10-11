"""The human labelling page: windows with input strips and replay links, answers saved as human labels,
and the server only answers requests addressed to localhost."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from paradigm_orion.labellers.human import Session, serve
from paradigm_orion.posthog import PostHog
from paradigm_orion.recipe import compile_recipe
from paradigm_orion.store import Store
from paradigm_orion.synthetic import generate, write_export
from paradigm_orion.workspace import Workspace, read_jsonl


class HumanPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        rows, _ = generate(people=3, sessions_per_person=1, seed=2)
        write_export(rows, root / "export.jsonl.gz")
        self.store = Store(Workspace(root / ".orion"))
        self.store.import_files([root / "export.jsonl.gz"], "import", ("$ai_generation",))
        self.store.index()
        self.recipe = compile_recipe({"name": "checkout-sentiment", "target": {"frustrated": "binary"}, "labels": {"by": ["human"]}})
        client = PostHog("https://us.posthog.com", "42", "phx_unused")
        self.session = Session(self.store, self.recipe, client, limit=20)
        self.server, self.url = serve(self.session, port=0, open_browser=False, log=lambda *a: None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, host=None):
        req = urllib.request.Request(self.url.rstrip("/") + path, headers={"Host": host} if host else {})
        with urllib.request.urlopen(req) as r:
            return r.read()

    def post(self, body, content_type="application/json"):
        req = urllib.request.Request(self.url + "api/label", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": content_type})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_windows_show_input_and_link_to_the_replay(self):
        state = json.loads(self.get("/api/state"))
        self.assertEqual(state["total"], 20)
        self.assertEqual(state["target"]["binary"], True)
        item = json.loads(self.get("/api/item/0"))
        self.assertTrue(item["replay_url"].startswith(f"https://us.posthog.com/project/42/replay/{item['session_id']}?t="))
        strip = item["strip"]
        self.assertEqual(len(strip["speed"]), 101)
        self.assertTrue(strip["keys"] or strip["clicks"] or any(strip["speed"]))
        self.assertIn(b"Orion labelling", self.get("/"))

    def test_answers_are_saved_as_human_labels_and_can_be_undone(self):
        item = json.loads(self.get("/api/item/3"))
        state = self.post({"id": item["id"], "value": "yes", "confidence": 0.6})
        self.assertEqual(state["done"], 1)
        saved = read_jsonl(self.session.path)
        self.assertEqual(len(saved), 1)
        label = saved[0]
        self.assertEqual((label["source"], label["labeller"], label["targets"]["frustrated"], label["confidence"]), ("human", "human", "yes", 0.6))
        self.assertEqual(label["end_ms"] - label["start_ms"], 10000)
        self.assertEqual(json.loads(self.get("/api/item/3"))["answer"], "yes")
        self.assertEqual(self.post({"id": item["id"], "value": None})["done"], 0)
        self.assertEqual(read_jsonl(self.session.path), [])
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.post({"id": item["id"], "value": "maybe"})
        self.assertEqual(bad.exception.code, 400)

    def test_requests_for_other_hosts_or_forms_are_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as rebinding:
            self.get("/api/state", host="attacker.example")
        self.assertEqual(rebinding.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as form:
            self.post({"id": "x", "value": "yes"}, content_type="text/plain")
        self.assertEqual(form.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
