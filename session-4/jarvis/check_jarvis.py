"""Tests for the Jarvis console server. Run: python jarvis/check_jarvis.py

The server runs inside the test process on a random port and stops when the tests end.
A fake skill stands in for the RAG pipeline, so no model, database or API key is needed.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import server  # noqa: E402
import skills  # noqa: E402


class FakeSkill(skills.Skill):  # emits the same event types as the RAG skill, using a real child process
    name, title, description, needs_question = "fake", "Fake", "test skill", True
    options = [{"key": "lines", "label": "Lines", "type": "int", "default": 3, "min": 1, "max": 50},
               {"key": "pause", "label": "Pause", "type": "bool", "default": False}]

    def validate(self, request):
        clean = super().validate(request)
        clean["question"] = str(request.get("question", "")).strip() or "q"
        return clean

    def stages(self, request):
        return [{"id": "work", "title": "Work"}]

    def run(self, request, run):
        run.emit("stage_start", stage="work", title="Work")
        sleep = "0.5" if request["pause"] else "0"
        code = run.run_process("work", [sys.executable, "-c",
                                        f"import time\nfor i in range({request['lines']}):\n    print('line', i, 'שלום')\n    time.sleep({sleep})"],
                               HERE, {})
        run.check_cancelled()
        run.emit("stage_end", stage="work", status="ok" if code == 0 else "error", code=code, seconds=0.1)
        run.emit("answer", claims=[{"text": "done", "citation_ids": ["c1"]}], missing_information=[],
                 sources=[{"chunk_id": "c1", "text": "t", "metadata": {}}], citation_status="ok",
                 invalid_source_ids=[], provider="fake", model="fake", seconds=0.1)


class JarvisServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        skills.SKILLS["fake"] = FakeSkill()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, cls.tmp.name)  # port 0 = any free port
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        skills.SKILLS.pop("fake", None)
        cls.tmp.cleanup()

    def request(self, path, body=None, method=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def json(self, path, body=None):
        code, raw, _ = self.request(path, body)
        return code, json.loads(raw)

    def events(self, run_id, start=0):  # read the SSE stream until run_end
        with urllib.request.urlopen(f"{self.base}/api/runs/{run_id}/events?from={start}", timeout=30) as response:
            self.assertIn("text/event-stream", response.headers["Content-Type"])
            events = []
            for line in response:
                line = line.decode("utf-8").strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
                    if events[-1]["type"] == "run_end":
                        return events
        return events

    def wait_idle(self):
        for _ in range(100):
            if not self.json("/api/runs")[1]["active"]:
                return
            time.sleep(0.1)

    def test_static_files_and_traversal(self):
        code, body, headers = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(b"JARVIS", body)
        self.assertEqual(self.request("/static/app.js")[0], 200)
        self.assertEqual(self.request("/static/style.css")[0], 200)
        for path in ("/static/../server.py", "/static/%2e%2e%5cserver.py", "/static/missing.js"):
            self.assertEqual(self.request(path)[0], 404, path)

    def test_skills_listed_with_options(self):
        code, data = self.json("/api/skills")
        self.assertEqual(code, 200)
        names = {s["name"]: s for s in data}
        self.assertIn("rag", names)
        self.assertIn("status", names)
        self.assertEqual({o["key"] for o in names["rag"]["options"]} >= {"provider", "source", "hybrid", "top_k"}, True)

    def test_local_source_skips_mongodb(self):  # pure step building: nothing is run
        import rag
        steps = {s["id"]: s["args"] for s in rag.build_steps("q", source="local")}
        self.assertIn("--no-atlas", steps["embed"])
        self.assertEqual(steps["retrieve"][steps["retrieve"].index("--source") + 1], "file")
        self.assertEqual(rag.build_steps("q"), rag.build_steps("q", source="local"))  # local is the default
        atlas = {s["id"]: s["args"] for s in rag.build_steps("q", source="mongodb")}
        self.assertNotIn("--no-atlas", atlas["embed"])
        self.assertNotIn("--source", atlas["retrieve"])
        self.assertEqual(self.json("/api/runs", {"skill": "rag", "request": {"question": "x", "source": "s3"}})[0], 400)

    def test_atlas_error_is_short_and_hides_hosts(self):
        import status
        safe = lambda e, s: f"{type(e).__name__}: {e}"
        timeout = type("ServerSelectionTimeoutError", (Exception,), {})("ac-x-shard-00-01.abc.mongodb.net:27017: SSL handshake failed " * 20)
        self.assertNotIn("mongodb.net", status.atlas_error_detail(timeout, {}, safe))
        other = status.atlas_error_detail(ValueError("bad host ac-x-shard-00-01.abc.mongodb.net:27017 " * 20), {}, safe)
        self.assertNotIn("mongodb.net", other)
        self.assertLessEqual(len(other), 160)

    def test_invalid_requests(self):
        self.assertEqual(self.json("/api/runs", {"skill": "nope", "request": {}})[0], 400)
        self.assertEqual(self.json("/api/runs", {"skill": "rag", "request": {"question": "  "}})[0], 400)
        self.assertEqual(self.json("/api/runs", {"skill": "rag", "request": {"question": "x", "top_k": 99}})[0], 400)
        self.assertEqual(self.json("/api/runs", {"skill": "rag", "request": {"question": "x", "provider": "openai"}})[0], 400)
        self.assertEqual(self.request("/api/runs/does-not-exist")[0], 404)
        self.assertEqual(self.request("/api/runs/..%2f..%2fserver")[0], 404)

    def test_run_streams_events_and_is_saved(self):
        self.wait_idle()
        code, data = self.json("/api/runs", {"skill": "fake", "request": {"question": "hello", "lines": 4}})
        self.assertEqual(code, 202)
        run_id = data["run_id"]
        events = self.events(run_id)
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "run_start")
        self.assertEqual(types[-1], "run_end")
        self.assertEqual(events[-1]["status"], "done")
        logs = [e["line"] for e in events if e["type"] == "log"]
        self.assertEqual(logs, [f"line {i} שלום" for i in range(4)])  # UTF-8 survives the child process and SSE
        self.assertEqual([e["seq"] for e in events], list(range(len(events))))
        resumed = self.events(run_id, start=3)  # reconnect from the middle
        self.assertEqual(resumed[0]["seq"], 3)
        self.assertTrue((Path(self.tmp.name) / f"{run_id}.json").exists())
        history = self.json("/api/runs")[1]["runs"]
        self.assertEqual(history[0]["id"], run_id)
        self.assertEqual(history[0]["headline"], "done")
        self.assertEqual(self.json(f"/api/runs/{run_id}")[1]["summary"]["status"], "done")

    def test_busy_and_cancel(self):
        self.wait_idle()
        code, data = self.json("/api/runs", {"skill": "fake", "request": {"lines": 40, "pause": True}})
        self.assertEqual(code, 202)
        run_id = data["run_id"]
        time.sleep(0.8)
        self.assertEqual(self.json("/api/runs")[1]["active"], run_id)
        self.assertEqual(self.json("/api/runs", {"skill": "fake", "request": {}})[0], 409)  # one run at a time
        started = time.time()
        self.assertEqual(self.json(f"/api/runs/{run_id}/cancel", {})[0], 200)
        events = self.events(run_id)
        self.assertEqual(events[-1]["status"], "cancelled")
        self.assertLess(time.time() - started, 5)  # the child process was terminated, not waited out
        self.assertFalse(any(e["type"] == "answer" for e in events))

    def test_status_never_leaks_secrets(self):
        code, raw, _ = self.request("/api/status?force=1", timeout=90)  # an unreachable Atlas takes ~30 s to time out
        self.assertEqual(code, 200)
        data = json.loads(raw)
        self.assertEqual(set(data["systems"]), {"ollama", "atlas", "anthropic", "index", "voice", "tts"})
        text = raw.decode("utf-8")
        for marker in ("mongodb+srv://", "mongodb://", "sk-ant-", "password", ".mongodb.net"):
            self.assertNotIn(marker, text)

    def test_transcribe_rejects_empty_audio(self):
        code, raw, _ = self.request("/api/transcribe", body=None, method="POST")
        self.assertEqual(code, 400)

    def test_speak_rejects_empty_text(self):  # validated before any model loads
        code, raw, _ = self.request("/api/speak", body={"text": "  "}, method="POST")
        self.assertEqual(code, 400)
        code, raw, _ = self.request("/api/speak", body={"text": "hello", "language": "xx"}, method="POST")
        self.assertEqual(code, 400)

    def test_hebrew_text_preparation(self):
        import tts
        self.assertEqual(tts.hebrew_number(123), "מאה עשרים ושלוש")
        self.assertEqual(tts.hebrew_number(2025), "אלפיים עשרים וחמש")
        self.assertEqual(tts.prepare("שגיאה 401 [c-2]", "he"), "שגיאה ארבע מאות ואחת")
        self.assertTrue(all(len(c) <= tts.CHUNK_CHARS for c in tts.chunks("מילה " * 200)))

    def test_english_inside_hebrew_is_spoken(self):  # Latin runs go to the English voice instead of being dropped
        import tts
        parts = tts.segments("שגיאות ב־Cedar הגיעו ל־94% HTTP 401 לפי SYS-001.", "he")
        self.assertIn(("en", "Cedar"), parts)
        self.assertIn(("en", "aitch tee tee pee"), parts)
        self.assertIn(("en", "ess why ess zero zero one"), parts)
        self.assertTrue(any(lang == "he" and "תשעים וארבע אחוז" in text for lang, text in parts))
        self.assertEqual(tts.prepare_english("notification_delay_p95_seconds"), "notification delay p ninety five seconds")
        self.assertEqual(tts.english_number(2026), "two thousand twenty six")


if __name__ == "__main__":
    unittest.main(verbosity=2)
