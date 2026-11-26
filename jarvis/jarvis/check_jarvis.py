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
        cls.router = skills.ROUTER
        skills.ROUTER = "rules"  # the tests below check the patterns; model routing has its own test (fake model)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, cls.tmp.name)  # port 0 = any free port
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        skills.SKILLS.pop("fake", None)
        skills.ROUTER = cls.router
        cls.tmp.cleanup()

    def request(self, path, body=None, method=None, timeout=30):
        raw = isinstance(body, bytes)  # raw bytes go as audio, anything else as JSON
        data = body if raw else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                     headers={"Content-Type": "audio/webm" if raw else "application/json"})
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
        self.assertEqual({o["key"] for o in names["shorts"]["options"]}, {"mode", "limit", "details"})
        self.assertEqual({o["key"] for o in names["rag"]["options"]} >= {"provider", "source", "hybrid", "top_k"}, True)

    def test_router_sends_each_request_to_the_right_skill(self):
        import router
        cases = {
            "find me 5 youtube shorts about cooking": ("shorts", "cooking", {"mode": "search", "limit": 5}),
            "תמצא לי שורטס על בישול": ("shorts", "בישול", {"mode": "search"}),
            "תראה לי 3 סרטונים של חתולים": ("shorts", "חתולים", {"mode": "search", "limit": 3}),
            "show me shorts of @MrBeast": ("shorts", "@MrBeast", {"mode": "channel"}),
            "@MrBeast": ("shorts", "@MrBeast", {"mode": "channel"}),
            "youtube shorts from channel MrBeast": ("shorts", "MrBeast", {"mode": "channel"}),
            "סרטונים מהערוץ MrBeast ביוטיוב": ("shorts", "MrBeast", {"mode": "channel"}),
            "what's this? https://youtu.be/dQw4w9WgXcQ": ("shorts", "https://youtu.be/dQw4w9WgXcQ", {"mode": "url"}),
            "/shorts lofi": ("shorts", "lofi", {"mode": "search"}),
            "system check": ("status", "", {}),
            "בדיקת מערכות": ("status", "", {}),
            "סנכרן את הכספת": ("vault_sync", "", {}),
            "/docs youtube strategy notes": ("rag", "youtube strategy notes", {}),
        }
        for text, (skill, question, options) in cases.items():
            got = router.route(text)
            self.assertEqual((got.skill, got.question, got.options), (skill, question, options), text)
        for text in ("מה הוחלט בישיבת הצוות על הסרטונים?", "what did we decide about the videos in the meeting?",
                     "who owns the youtube channel project?", "מי אחראי על ערוץ היוטיוב?", "cat videos",
                     "email me at a@b.com about the 5 tasks", "What was the error rate in September?"):
            got = router.route(text)
            self.assertEqual((got.skill, got.question), ("rag", text), text)  # questions about the notes stay with the notes

    def test_router_chains_notes_and_youtube(self):  # a YouTube request about something in the notes -> combo
        import router
        cases = {
            "מה הצוות אוהב לאכול? תמצא לי שורטס על זה": "מה הצוות אוהב לאכול",
            "מה הצוות אוהב לאכול ותביא לי סרטונים": "מה הצוות אוהב לאכול",
            "תמצא לי שורטס על מה שהצוות אוהב לאכול": "מה שהצוות אוהב לאכול",
            "what does the team like to eat and find shorts about it": "what does the team like to eat",
        }
        for text, question in cases.items():
            got = router.route(text)
            self.assertEqual((got.skill, got.question), ("combo", question), text)
        self.assertEqual(router.route("find shorts about teamwork").skill, "shorts")  # no notes topic: YouTube only

    def test_combo_answers_from_notes_then_searches_shorts(self):  # RAG and the MCP call are stubbed
        import rag
        calls = []

        class FakeRag(skills.RagSkill):
            def stages(self, request):
                return [{"id": "answer", "title": "Answer"}]

            def run(self, request, run):
                calls.append(("rag", request["question"]))
                run.emit("answer", claims=[{"text": "The team loves pizza [S1].", "citation_ids": []}], missing_information=[],
                         sources=[], citation_status="ok", invalid_source_ids=[], provider="ollama", model="m", seconds=0)

        class FakeShorts(skills.ShortsSkill):
            def run(self, request, run):
                calls.append(("shorts", request["question"]))
                run.emit("posts", tool="search_youtube_shorts", query=request["question"], posts=[])

        with tempfile.TemporaryDirectory() as folder:
            answer = Path(folder) / "answer.json"
            answer.write_text(json.dumps({"answer": {"claims": [{"text": "The team loves pizza [S1]."}]}}), encoding="utf-8")
            saved = skills.SKILLS["rag"], skills.SKILLS["shorts"], rag.ANSWER, skills.search_query
            skills.SKILLS["rag"], skills.SKILLS["shorts"], rag.ANSWER = FakeRag(), FakeShorts(), answer
            skills.search_query = lambda *a, **k: "pizza recipe"
            try:
                self.wait_idle()
                code, data = self.json("/api/runs", {"skill": "jarvis", "request": {
                    "question": "what does the team like to eat? find shorts about it"}})
                self.assertEqual(code, 202)
                events = self.events(data["run_id"])
                self.assertEqual([s["id"] for s in events[0]["stages"]], ["route", "answer", "query", "mcp"])
                self.assertEqual(calls, [("rag", "what does the team like to eat"), ("shorts", "pizza recipe")])
                skills.search_query = lambda *a, **k: 1 / 0  # model down: search with the answer's own words
                calls.clear()
                self.wait_idle()
                code, data = self.json("/api/runs", {"skill": "jarvis", "request": {
                    "question": "what does the team like to eat? find shorts about it"}})
                self.events(data["run_id"])
                self.assertEqual(calls[-1], ("shorts", "The team loves pizza"))
            finally:
                skills.SKILLS["rag"], skills.SKILLS["shorts"], rag.ANSWER, skills.search_query = saved

    def test_conversation_makes_follow_ups_work(self):  # history from the console: rewrite + "shorts about it"
        history = [{"question": "מה עידן אוהב לאכול?", "answer": "עידן עף על פיצות."}]
        rag_request = skills.SKILLS["rag"].validate({"question": "והראל?", "history": history})
        self.assertTrue(rag_request["rewrite"])
        self.assertEqual(skills.SKILLS["rag"].stages(rag_request)[0]["id"], "context")  # "Understand follow-up" first
        self.assertFalse(skills.SKILLS["rag"].validate({"question": "מה סוכם בישיבת הצוות ב-11.11?", "history": history})["rewrite"])
        self.assertEqual(skills.clean_history([{"question": "q", "answer": "a" * 5000}, "junk", {"question": "", "answer": "x"}]),
                         [{"question": "q", "answer": "a" * skills.MAX_TURN_CHARS}])
        with self.assertRaises(ValueError):  # "about it" with nothing before it
            skills.SKILLS["jarvis"].validate({"question": "find shorts about it"})
        calls = []

        class FakeShorts(skills.ShortsSkill):
            def run(self, request, run):
                calls.append(request["question"])
                run.emit("posts", tool="search_youtube_shorts", query=request["question"], posts=[])

        saved = skills.SKILLS["shorts"], skills.search_query
        skills.SKILLS["shorts"] = FakeShorts()
        skills.search_query = lambda asked, claims, *a: "pizza" if claims == ["עידן עף על פיצות."] else "wrong"
        try:
            self.wait_idle()
            code, data = self.json("/api/runs", {"skill": "jarvis", "request": {"question": "תמצא לי שורטס על זה", "history": history}})
            self.assertEqual(code, 202)
            events = self.events(data["run_id"])
        finally:
            skills.SKILLS["shorts"], skills.search_query = saved
        self.assertEqual([s["id"] for s in events[0]["stages"]], ["route", "query", "mcp"])  # no new notes search
        self.assertEqual(calls, ["pizza"])  # searched from the previous answer

    def test_youtube_topic_from_the_notes_asks_the_notes_first(self):  # "find shorts about Idan", Idan is in a note
        import rag
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            (vault / "Team").mkdir()
            (vault / "Team" / "food.md").write_text("# אוכל\n\nעידן עף על פיצות.\n", encoding="utf-8")
            saved_vault, rag.VAULT = rag.VAULT, vault
            try:
                route = skills.SKILLS["jarvis"].validate({"question": "תמצא לי שורטס על עידן"})["route"]
                self.assertEqual((route["skill"], route["request"]["question"], route["fallback"]),
                                 ("combo", "מה כתוב בפתקים על עידן?", "עידן"))
                self.assertEqual(skills.SKILLS["jarvis"].validate({"question": "תמצא לי שורטס על ולעידן"})["route"]["skill"],
                                 "combo")  # Hebrew prefix letters
                for text in ("find shorts about cooking", "/shorts עידן"):  # not in the notes / forced YouTube only
                    self.assertEqual(skills.SKILLS["jarvis"].validate({"question": text})["route"]["skill"], "shorts", text)
            finally:
                rag.VAULT = saved_vault

    def test_hebrew_names_find_english_names_in_the_notes(self):  # "עומר ברנשטיין" -> "Omer Bernstein" in a chat export
        import rag
        self.assertEqual(skills.sound_key("ברנשטיין"), skills.sound_key("Bernstein"))
        self.assertEqual(skills.sound_key("נזרצקי"), skills.sound_key("Nazarestsky"))
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            chat = "\n".join(f"[11:{i:02d}] **Omer Bernstein:** message {i}" for i in range(6))
            (vault / "chat.md").write_text("# Chat\n\n" + chat + "\n[12:00] **Yarden Nazarestsky:** hi\n", encoding="utf-8")
            saved_vault, rag.VAULT = rag.VAULT, vault
            skills._vault_words["snapshot"] = None
            try:
                self.assertEqual(skills.vault_mentions("עומר ברנשטיין"), ["עומר", "ברנשטיין"])
                self.assertEqual(skills.vault_mentions("נזרצקי"), ["נזרצקי"])
                self.assertEqual(skills.vault_mentions("עומר"), ["עומר"])  # short, but a frequent chat participant
                self.assertEqual(skills.vault_mentions("בישול"), [])
                route = skills.SKILLS["jarvis"].validate({"question": "תיצור לי בדיחה על עומר ברנשטיין"})["route"]
                self.assertEqual((route["skill"], route["request"]["notes"]["question"]), ("meme", "עומר ברנשטיין"))
                stages = skills.SKILLS["jarvis"].stages({"route": route})
                self.assertEqual([s["id"] for s in stages][-3:], ["retrieve", "plan", "mcp"])  # the notes, then the meme
                self.assertIsNone(skills.SKILLS["jarvis"].validate({"question": "תספר לי בדיחה"})["route"]["request"]["notes"])
            finally:
                rag.VAULT = saved_vault
                skills._vault_words["snapshot"] = None

    def test_requests_that_point_back_use_the_conversation(self):
        import router
        for text in ("תביא לי סרטון שמייצג את היחסים ביניהם", "תראה לי סרטון שקשור לסיטואציה הזאת", "find a video about this situation"):
            got = router.route(text)
            self.assertEqual((got.skill, got.question, got.options.get("follow_up")), ("combo", "", True), text)
        got = router.route("תחפש לי מידע על עילאי רז ואז תביא לי סרטון על המידע הזה")  # an explicit chain: notes, then YouTube
        self.assertEqual((got.skill, got.question), ("combo", "תחפש לי מידע על עילאי רז"))
        self.assertEqual(router.route("לימים הוא הבין").skill, "rag")  # "לימים" without a make verb is just a word
        history = [{"question": "q", "answer": "a"}]
        self.assertFalse(skills.needs_rewrite("מה הצוות אוהב לאכול", history))  # stands alone: no extra model call
        self.assertTrue(skills.needs_rewrite("ומה עם ירדן?", history))

    def test_vault_scope_defaults_to_whole_vault(self):  # a note in any folder, a new one too, is indexed
        support = sys.modules.get("index_support") or __import__("index_support")
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            for rel in ("projects/p.md", "Team likes/food.md", "meeting.md", ".obsidian/x.md", "Templates/t.md"):
                (vault / rel).parent.mkdir(parents=True, exist_ok=True)
                (vault / rel).write_text("# note\nbody", encoding="utf-8")
            files, scope = support.vault_files(vault, ())
            names = sorted(f.relative_to(vault).as_posix() for f in files)
            self.assertEqual((scope, names), ("whole vault", ["Team likes/food.md", "meeting.md", "projects/p.md"]))

    def test_ask_jarvis_routes_and_runs_the_target_skill(self):  # the Shorts skill is stubbed: no network
        class FakeShorts(skills.ShortsSkill):
            def run(self, request, run):
                run.emit("stage_start", stage="mcp", title="MCP · youtube-shorts")
                run.emit("stage_end", stage="mcp", status="ok", seconds=0)
                run.emit("posts", tool="search_youtube_shorts", query=request["question"],
                         posts=[{"title": f"{request['question']} x{request['limit']}", "url": "https://youtube.com/shorts/x"}])
        real = skills.SKILLS["shorts"]
        skills.SKILLS["shorts"] = FakeShorts()
        try:
            self.wait_idle()
            code, data = self.json("/api/runs", {"skill": "jarvis", "request": {"question": "find 4 shorts about cats"}})
            self.assertEqual(code, 202)
            events = self.events(data["run_id"])
        finally:
            skills.SKILLS["shorts"] = real
        start = events[0]
        self.assertEqual([s["id"] for s in start["stages"]], ["route", "mcp"])  # the monitor shows the routed pipeline
        self.assertEqual(start["request"]["question"], "find 4 shorts about cats")  # history keeps what was asked
        route = next(e for e in events if e["type"] == "route")
        self.assertEqual((route["skill"], route["title"]), ("shorts", "YouTube Shorts"))
        posts = next(e for e in events if e["type"] == "posts")
        self.assertEqual(posts["posts"][0]["title"], "cats x4")
        self.assertEqual(events[-1]["status"], "done")
        names = {s["name"]: s for s in self.json("/api/skills")[1]}
        self.assertEqual(list(names)[0], "jarvis")  # first = the console's default
        self.assertTrue({"provider", "top_k", "limit", "details"} <= {o["key"] for o in names["jarvis"]["options"]})
        # a question goes to the documents, with the documents' stages; a YouTube request without a topic is rejected early
        clean = skills.SKILLS["jarvis"].validate({"question": "מה הוחלט בישיבה?"})
        self.assertEqual(clean["route"]["skill"], "rag")
        self.assertEqual(skills.SKILLS["jarvis"].stages(clean)[1:], skills.SKILLS["rag"].stages(clean["route"]["request"]))
        self.assertEqual(self.json("/api/runs", {"skill": "jarvis", "request": {"question": "find shorts"}})[0], 400)
        self.assertEqual(self.json("/api/runs", {"skill": "jarvis", "request": {"question": "  "}})[0], 400)

    def test_model_picks_the_tool(self):  # "Route with: model": the answer model is stubbed, no network
        import router
        jarvis = skills.SKILLS["jarvis"]
        for text, skill in (("system check", "status"), ("@MrBeast", "shorts"), ("/meme cats", "meme"),
                            ("https://youtu.be/dQw4w9WgXcQ", "shorts")):  # sure cases: no model call
            self.assertEqual(jarvis.validate({"question": text, "router": "model"})["route"]["skill"], skill, text)
        pending = jarvis.validate({"question": "find 4 shorts about cats", "router": "model"})
        self.assertEqual((pending["route"]["skill"], jarvis.stages(pending)), ("pending", [{"id": "route", "title": "Understand request"}]))
        # the model's choice -> the same routes the rules build
        self.assertEqual(router.from_tool("notes_then_shorts", "", "find a video about it").options.get("follow_up"), True)
        self.assertEqual(router.from_tool("notes_then_shorts", "מה הצוות אוהב לאכול?", "x").question, "מה הצוות אוהב לאכול?")
        self.assertEqual(router.from_tool("make_meme", "cats", "make a meme about cats").question, "make a meme about cats")
        self.assertEqual(router.from_tool("ask_notes", "rewritten", "מה הוחלט?").question, "מה הוחלט?")  # as asked
        with self.assertRaises(ValueError):
            router.from_tool("delete_everything", "", "x")
        prompt = router.tool_prompt("and him?", [{"question": "who likes pizza?", "answer": "Idan"}],
                                    lambda server, tool: "LIVE " + tool if server == "memes" else None)
        self.assertIn("Request: and him?", prompt)
        self.assertIn("Previous answer: Idan", prompt)
        self.assertIn("make_meme(input: what the meme is about): LIVE generate_meme\n", prompt)  # the MCP server's own words
        self.assertIn("Search YouTube Shorts by keyword", prompt)  # no live text: the catalog's

        calls, replies = [], []

        class FakeShorts(skills.ShortsSkill):
            def run(self, request, run):
                calls.append((request["question"], request["mode"], request["limit"]))
                run.emit("stage_start", stage="mcp", title="MCP · youtube-shorts")
                run.emit("stage_end", stage="mcp", status="ok", seconds=0)
                run.emit("posts", tool="search_youtube_shorts", query=request["question"], posts=[])

        def fake_ask_json(prompt, schema, provider, model, timeout=90, strict=True):
            self.assertEqual(schema["properties"]["tool"]["enum"], router.TOOL_NAMES)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        saved = skills.SKILLS["shorts"], skills.ask_json
        skills.SKILLS["shorts"], skills.ask_json = FakeShorts(), fake_ask_json
        try:
            runs = []
            for reply, text in (({"tool": "search_youtube_shorts", "input": "cute cats"}, "find 4 shorts about cats"),
                                (OSError("Ollama is off"), "find 4 shorts about cats"),  # the rules take over
                                ({"tool": "notes_then_shorts", "input": ""}, "find shorts about it")):  # nothing before "it"
                replies.append(reply)
                self.wait_idle()
                code, data = self.json("/api/runs", {"skill": "jarvis", "request": {"question": text, "router": "model"}})
                self.assertEqual(code, 202, text)
                runs.append(self.events(data["run_id"]))
        finally:
            skills.SKILLS["shorts"], skills.ask_json = saved
        model_run, fallback_run, rejected = runs
        self.assertEqual(calls, [("cute cats", "search", 4), ("cats", "search", 4)])
        self.assertEqual([s["id"] for s in model_run[0]["stages"]], ["route"])  # known before the model answers
        self.assertEqual([s["id"] for s in next(e for e in model_run if e["type"] == "stages")["stages"]], ["route", "mcp"])
        self.assertIn("search_youtube_shorts", next(e for e in model_run if e["type"] == "route")["reason"])
        ends = {e["stage"]: e["status"] for run in (model_run, fallback_run) for e in run if e["type"] == "stage_end"}
        self.assertEqual(next(e["status"] for e in fallback_run if e["type"] == "stage_end" and e["stage"] == "route"), "warning")
        self.assertEqual(ends["mcp"], "ok")
        self.assertTrue(any("used the rules" in e.get("line", "") for e in fallback_run))
        self.assertEqual(rejected[-1]["status"], "error")
        self.assertIn("About what?", next(e for e in rejected if e["type"] == "error")["message"])

    def test_meme_requests_route_to_the_memes_skill(self):
        import router
        for text in ("make a meme about monday meetings", "drake meme: writing tests / shipping on friday", "memes",
                     "תעשה לי מם על ישיבות", "תכין מם של דרייק: לכתוב בדיקות / לשחרר בשישי", "/meme cats"):
            self.assertEqual(router.route(text).skill, "meme", text)
        for text, skill in (("what did we decide about the meme campaign?", "rag"), ("מה הוחלט על המם?", "rag"),
                            ("who likes memes in the team?", "rag"), ("find shorts about memes", "shorts")):
            self.assertEqual(router.route(text).skill, skill, text)  # questions about memes are not meme requests
        templates = [{"id": "drake", "name": "Drakeposting", "lines": 2}, {"id": "fry", "name": "Futurama Fry", "lines": 2},
                     {"id": "bad", "name": "Bad Luck Brian", "lines": 2}]
        self.assertEqual(skills.meme_template("drake meme: a / b", templates), "drake")
        self.assertEqual(skills.meme_template("a futurama fry about bugs", templates), "fry")
        self.assertIsNone(skills.meme_template("make a meme about my bad day", templates))  # "bad" alone is a word, not a pick
        self.assertEqual(skills.meme_captions("drake meme: writing tests / shipping on friday"), ["writing tests", "shipping on friday"])
        self.assertEqual(skills.meme_captions('a fry meme "not sure if bug" "or feature"'), ["not sure if bug", "or feature"])
        self.assertEqual(skills.meme_captions("make a meme about cats"), [])
        clean = skills.SKILLS["jarvis"].validate({"question": "make a meme about cats"})
        self.assertEqual(clean["route"]["skill"], "meme")
        ids = [s["id"] for s in skills.SKILLS["jarvis"].stages(clean)]
        self.assertEqual((ids[0], ids[-3:]), ("route", ["retrieve", "plan", "mcp"]))  # the notes are searched first
        self.assertNotIn("answer", ids)  # retrieval only: no full RAG answer before a meme
        self.assertEqual(clean["route"]["request"]["notes"]["question"], "cats")  # the topic, not the command words
        off = skills.SKILLS["jarvis"].validate({"question": "make a meme about cats", "use_notes": False})
        self.assertEqual([s["id"] for s in skills.SKILLS["jarvis"].stages(off)], ["route", "plan", "mcp"])
        own = skills.SKILLS["jarvis"].validate({"question": "drake meme: a / b"})  # captions given: nothing to look up
        self.assertIsNone(own["route"]["request"]["notes"])
        self.assertEqual(self.json("/api/runs", {"skill": "meme", "request": {"question": "x", "template": "../x"}})[0], 400)

    def test_joke_requests_get_a_meme(self):  # "tell me a joke" -> the memes MCP tool
        import router
        for text, topic in (("tell me a joke about programmers", "programmers"), ("תספר לי בדיחה על מתכנתים", "מתכנתים"),
                            ("make me laugh", "make me laugh"), ("תצחיק אותי", "תצחיק אותי"), ("יש לך בדיחה?", "יש לך בדיחה?"),
                            ("do you know any jokes?", "do you know any jokes?"), ("בדיחה על ישיבות", "ישיבות")):
            self.assertEqual(router.route(text).skill, "meme", text)
            self.assertEqual(skills._meme_topic(text), topic, text)
        for text in ("תעשה לי מים על בצלאל זינגר", "תיצור לימים על ישיבות", "תכין לי ממים על מתכנתים"):  # how speech-to-text spells it
            self.assertEqual(router.route(text).skill, "meme", text)
        for text in ("תביא לי מים", "איפה המים?", "שתיתי מים על הבוקר"):  # water stays water
            self.assertNotEqual(router.route(text).skill, "meme", text)
        self.assertEqual(skills.SKILLS["jarvis"].validate({"question": "תספר לי בדיחה"})["route"]["skill"], "meme")
        for text, skill in (("what joke did Idan tell at the meeting?", "rag"), ("איזו בדיחה עידן סיפר בישיבה?", "rag"),
                            ("מה הבדיחה מהישיבה?", "rag"), ("find shorts of jokes", "shorts")):
            self.assertEqual(router.route(text).skill, skill, text)  # questions about a joke stay with the notes

    def test_hebrew_meme_words_typed_together(self):  # "תיצור לימים על..." = "תיצור לי מימים על..."
        import router
        for text in ("תיצור לימים על עומר ברנשטיין", "תעשה מימים על הצוות", "תכין לממים על הישיבה"):
            self.assertEqual(router.route(text).skill, "meme", text)
        for text in ("לימים הוא עזב את הצוות", "תביא לי מים", "מה הוחלט על המם?"):  # "later on", "bring me water", a question
            self.assertNotEqual(router.route(text).skill, "meme", text)
        self.assertEqual(skills._meme_topic("תיצור לימים על עומר ברנשטיין"), "עומר ברנשטיין")

    def test_meme_captions_are_written_from_the_notes(self):  # the model sees the note excerpts; no network
        sent = {}

        class Reply:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.body

        def fake_urlopen(call, timeout=None):
            sent.update(json.loads(call.data.decode("utf-8")))
            return Reply(json.dumps({"message": {"content": json.dumps({"template_id": "drake", "lines": ["א", "ב"]})}}).encode())
        templates = [{"id": "drake", "name": "Drakeposting", "lines": 2}]
        real = skills.urllib.request.urlopen
        skills.urllib.request.urlopen = fake_urlopen
        try:
            facts = [("התינוקייה", "עומר שאל אם מישהו קיבל זימון לראיון"), ("Meeting 11.11", "Omer brought pizza")]
            template, lines, _ = skills.SKILLS["meme"].write("עומר ברנשטיין", None, templates, "ollama", facts)
        finally:
            skills.urllib.request.urlopen = real
        prompt = sent["messages"][0]["content"]
        self.assertIn("עומר שאל אם מישהו קיבל זימון לראיון", prompt)
        self.assertIn("Omer brought pizza", prompt)
        self.assertIn("Hebrew", prompt)
        self.assertEqual((template, lines), ("drake", ["א", "ב"]))
        self.assertEqual(sent["format"]["properties"]["lines"]["minItems"], 2)

    def test_ask_jarvis_runs_the_meme_skill(self):  # MCP and model stubbed: no network
        class FakeMeme(skills.MemeSkill):
            def run(self, request, run):
                run.emit("stage_start", stage="plan", title="Plan the meme")
                run.emit("stage_end", stage="plan", status="ok", seconds=0)
                run.emit("meme", image="https://api.memegen.link/images/drake/a/b.png", template="drake", name="Drakeposting",
                         text=["a", "b"], renderer="memegen", hebrew=False)
        real = skills.SKILLS["meme"]
        skills.SKILLS["meme"] = FakeMeme()
        try:
            self.wait_idle()
            code, data = self.json("/api/runs", {"skill": "jarvis", "request": {"question": "drake meme: a / b"}})
            self.assertEqual(code, 202)
            events = self.events(data["run_id"])
        finally:
            skills.SKILLS["meme"] = real
        self.assertEqual(next(e for e in events if e["type"] == "route")["skill"], "meme")
        self.assertEqual(next(e for e in events if e["type"] == "meme")["template"], "drake")
        self.assertEqual(events[-1]["status"], "done")

    def test_media_serves_only_meme_files(self):  # locally drawn (Hebrew) memes; nothing else is reachable
        media = server.MEDIA
        media.mkdir(parents=True, exist_ok=True)
        name = "0123456789abcdef.png"
        (media / name).write_bytes(b"\x89PNG fake")
        try:
            code, body, headers = self.request(f"/media/{name}")
            self.assertEqual((code, headers["Content-Type"], body), (200, "image/png", b"\x89PNG fake"))
            for path in ("/media/../server.py", "/media/%2e%2e%5cserver.py", "/media/0123456789abcdef.py",
                         "/media/ffffffffffffffff.png", "/media/x.png", "/media/0123456789ABCDEF.png"):
                self.assertEqual(self.request(path)[0], 404, path)
        finally:
            (media / name).unlink()

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

    def test_evidence_carries_the_chunk_text(self):  # clicking an Evidence item shows the chunk in a popup
        import rag
        result = {"rank": 1, "chunk_id": "c1", "score": 0.9, "text": "הצוות אוהב פיצה.\nSecond line.",
                  "metadata": {"document_id": "D1", "version": 1, "status": "active", "source": "Meetings/a.md"}}
        events = []

        class Run:
            def emit(self, kind, **data): events.append((kind, data))

        saved = rag.SEARCH_RESULTS
        with tempfile.TemporaryDirectory() as tmp:
            rag.SEARCH_RESULTS = Path(tmp) / "search-results.json"
            rag.SEARCH_RESULTS.write_text(json.dumps({"results": [result], "retrieval": {"method": "cosine_similarity"}}),
                                          encoding="utf-8")
            try:
                skills.RagSkill.emit_retrieval(Run())
            finally:
                rag.SEARCH_RESULTS = saved
        kind, data = events[0]
        self.assertEqual(kind, "retrieval")
        self.assertEqual(data["results"][0]["text"], result["text"])
        self.assertEqual(data["results"][0]["source"], "Meetings/a.md")

    def test_vault_loader_keeps_path_date_and_project(self):  # Obsidian notes: only Projects + Meetings, metadata kept
        import rag
        sys.path.insert(0, str(rag.EX1))
        import index_support, importlib.util
        with tempfile.TemporaryDirectory() as vault:
            root = Path(vault)
            for folder in ("Projects", "Meetings", "Drafts", "Templates", ".obsidian"):
                (root / folder).mkdir()
            (root / "Meetings" / "m1.md").write_text("---\nproject_id: P-7\nmeeting_date: 2026-11-01\ntags:\n  - meeting\n---\n"
                                                     "# Kickoff\n\nCheck name: oren-7\n", encoding="utf-8")
            (root / "Projects" / "p.md").write_text("# Project\n\nGoal.\n", encoding="utf-8")
            for skipped in ("Drafts/d.md", "Templates/t.md", ".obsidian/x.md", "indexing-scope.md", "loose.md"):
                (root / skipped).write_text("# skip\n\nno\n", encoding="utf-8")
            documents = {d["source"]: d for d in index_support.load_vault(root, ("Projects", "Meetings"))}  # narrowed scope
            self.assertEqual(set(documents), {"vault/Meetings/m1.md", "vault/Projects/p.md"})
            meeting = documents["vault/Meetings/m1.md"]
            self.assertEqual((meeting["title"], meeting["project_id"], meeting["meeting_date"], meeting["updated"]),
                             ("Kickoff", "P-7", "2026-11-01", "2026-11-01"))
            spec = importlib.util.spec_from_file_location("chunking_t", rag.EX1 / "chunking.py")
            chunking = importlib.util.module_from_spec(spec); spec.loader.exec_module(chunking)
            pieces = chunking.split_document(meeting)
            index_support.validate_pieces(meeting, pieces, 900)
            metadata = chunking.make_metadata(meeting, pieces[0])
            self.assertEqual(metadata["path"], "Meetings/m1.md")
            self.assertEqual(metadata["project_id"], "P-7")
        steps = {s["id"]: s["args"] for s in rag.build_steps("q", documents="vault")}
        self.assertIn("--vault", steps["chunk"])
        self.assertIn("--no-course-docs", steps["chunk"])
        self.assertNotIn("--vault", {s["id"]: s["args"] for s in rag.build_steps("q", documents="course")}["chunk"])

    def test_vault_changes_make_index_stale_and_watcher_syncs(self):  # live update: new / edited / deleted notes
        import subprocess, rag, vault_watch
        saved = rag.VAULT, rag.INDEX_FILE, vault_watch.POLL, vault_watch.SETTLE, dict(skills.LAST_INDEX)
        with tempfile.TemporaryDirectory() as tmp:
            vault, build = Path(tmp) / "vault", Path(tmp) / "build"
            (vault / "Meetings").mkdir(parents=True)
            note = vault / "Meetings" / "m1.md"
            note.write_text("# One\n\nfirst\n", encoding="utf-8")
            chunks, index = build / "chunks.json", build / "offline-index.json"
            env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
            def build_index():
                for args in (["prepare", "--vault", str(vault), "--no-course-docs", "--output", str(chunks)],
                             ["embed", "--fixture", "--input", str(chunks), "--output", str(index)]):
                    subprocess.run([sys.executable, "build_index.py", *args], cwd=rag.EX1, env=env, check=True,
                                   capture_output=True)
            try:
                rag.VAULT, rag.INDEX_FILE = vault, index
                build_index()
                self.assertFalse(rag.index_is_stale("vault"))
                self.assertTrue(rag.index_is_stale("both"))  # other documents requested
                self.assertEqual(rag.build_steps("q", skip_index=True)[0]["id"], "retrieve")  # fresh: indexing skipped
                (vault / "Meetings" / "m2.md").write_text("# Two\n\nnew note\n", encoding="utf-8")
                self.assertTrue(rag.index_is_stale("vault"))  # new note
                self.assertEqual(rag.build_steps("q", skip_index=True)[0]["id"], "chunk")  # stale: re-index first
                build_index()
                self.assertFalse(rag.index_is_stale("vault"))
                note.write_text("# One\n\nedited\n", encoding="utf-8")
                self.assertTrue(rag.index_is_stale("vault"))  # edited note
                build_index()
                (vault / "Meetings" / "m2.md").unlink()
                self.assertTrue(rag.index_is_stale("vault"))  # deleted note

                started = []
                class FakeManager:
                    def start(self, name, request):
                        started.append((name, request))
                        return type("R", (), {"status": "done", "id": "fake"})()
                vault_watch.POLL, vault_watch.SETTLE = 0.05, 0.1
                skills.LAST_INDEX.update(documents="vault", source="local")
                threading.Thread(target=vault_watch._loop, args=(FakeManager(),), daemon=True).start()
                deadline = time.time() + 5
                while not started and time.time() < deadline:
                    time.sleep(0.05)
                self.assertEqual(started[:1], [("vault_sync", {"documents": "vault", "source": "local"})])
            finally:
                rag.VAULT, rag.INDEX_FILE, vault_watch.POLL, vault_watch.SETTLE = saved[:4]
                skills.LAST_INDEX.update(saved[4])
                vault_watch.POLL = 3600  # park the test watcher thread

    def test_atlas_error_is_short_and_hides_hosts(self):
        import status
        safe = lambda e, s: f"{type(e).__name__}: {e}"
        timeout = type("ServerSelectionTimeoutError", (Exception,), {})("ac-x-shard-00-01.abc.mongodb.net:27017: SSL handshake failed " * 20)
        self.assertNotIn("mongodb.net", status.atlas_error_detail(timeout, {}, safe))
        other = status.atlas_error_detail(ValueError("bad host ac-x-shard-00-01.abc.mongodb.net:27017 " * 20), {}, safe)
        self.assertNotIn("mongodb.net", other)
        self.assertLessEqual(len(other), 160)

    def stored_run(self, run_id, skill, events, request):  # a finished run on disk, as the history keeps it
        summary = {"id": run_id, "skill": skill, "question": request.get("question", ""), "status": "done",
                   "started": time.time(), "seconds": 1.0, "headline": "", "feedback": None}
        path = Path(self.tmp.name) / f"{run_id}.json"
        path.write_text(json.dumps({"summary": summary, "request": request, "events": events}), encoding="utf-8")
        self.addCleanup(path.unlink)

    def test_judge_grades_a_finished_answer_with_its_own_model(self):  # LLM as a judge; the model call is stubbed
        import runs
        self.stored_run("20260101-000000-aaaa", "jarvis", JUDGED_ANSWER_EVENTS, {"question": "who handles auth errors?",
                                                                               "provider": "anthropic", "model": ""})
        calls = []

        def fake_ask(prompt, schema, provider, model, **kw):
            calls.append((provider, model, kw.get("max_tokens"), prompt))
            return JUDGE_REPLY
        saved, saved_save, stored = skills.ask_json, skills.JudgeSkill.save, []
        skills.ask_json = fake_ask
        skills.JudgeSkill.save = classmethod(lambda cls, doc: stored.append(doc) or "rag_session4.judgements")  # no real Atlas
        try:
            self.wait_idle()
            code, data = self.json("/api/runs", {"skill": "judge", "request": {"run_id": "20260101-000000-aaaa"}})
            self.assertEqual(code, 202)
            events = self.events(data["run_id"])
            self.assertEqual([s["id"] for s in events[0]["stages"]], ["collect", "judge", "save"])
            verdict = next(e for e in events if e["type"] == "judgement")
            self.assertEqual((verdict["provider"], verdict["model"], verdict["same_model"]), ("ollama", "gemma3:4b", True))  # the answer's model
            self.assertEqual((verdict["overall"], verdict["target_run"]), (4.3, "20260101-000000-aaaa"))
            self.assertEqual(calls[0][2], 2000)  # room for a verdict per claim
            self.assertIn("who handles auth errors?", calls[0][3])
            self.assertEqual(self.json(f"/api/runs/{data['run_id']}")[1]["summary"]["headline"], "Judge score 4.3/5")
            self.assertEqual(next(e for e in events if e["type"] == "judgement_saved")["saved_to"], "rag_session4.judgements")
            doc = stored[0]  # what goes to MongoDB: one document per judgement, keyed by the judge run's id
            self.assertEqual((doc["_id"], doc["target_run"], doc["target_skill"], doc["route"], doc["overall"], doc["model"]),
                             (data["run_id"], "20260101-000000-aaaa", "jarvis", "rag", 4.3, "gemma3:4b"))
            self.assertEqual(len(doc["claims"]), 2)
            self.assertIsNotNone(doc["judged_at"].tzinfo)
            skills.JudgeSkill.save = classmethod(lambda cls, doc: (_ for _ in ()).throw(RuntimeError("cannot reach the cluster")))
            self.wait_idle()
            code, data = self.json("/api/runs", {"skill": "judge", "request": {"run_id": "20260101-000000-aaaa"}})
            events = self.events(data["run_id"])  # the database is down: the scorecard still counts
            self.assertEqual(events[-1]["status"], "done")
            self.assertEqual(next(e for e in events if e["type"] == "judgement_saved")["error"], "cannot reach the cluster")
            self.assertEqual(next(e for e in events if e["type"] == "stage_end" and e["stage"] == "save")["status"], "warning")
            skills.ask_json = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused"))  # the model is down
            self.wait_idle()
            code, data = self.json("/api/runs", {"skill": "judge", "request": {"run_id": "20260101-000000-aaaa"}})
            events = self.events(data["run_id"])
            self.assertEqual(events[-1]["status"], "error")
            self.assertTrue(next(e for e in events if e["type"] == "error")["message"].startswith("Judge failed — ConnectionError"))
        finally:
            skills.ask_json, skills.JudgeSkill.save = saved, saved_save
        self.assertNotIn("judge", {s["name"] for s in self.json("/api/skills")[1]})  # started from a card, not the picker
        self.stored_run("20260101-000000-bbbb", "status", [{"type": "message", "text": "All systems are ready."}], {})
        busy = runs.Run("rag", {"question": "q"})
        server.Handler.manager.runs[busy.id] = busy  # still running
        try:
            for run_id in ("does-not-exist", "../x", "20260101-000000-bbbb", busy.id):
                self.assertEqual(self.json("/api/runs", {"skill": "judge", "request": {"run_id": run_id}})[0], 400, run_id)
        finally:
            server.Handler.manager.runs.pop(busy.id)

    def test_invalid_requests(self):
        self.assertEqual(self.json("/api/runs", {"skill": "nope", "request": {}})[0], 400)
        self.assertEqual(self.json("/api/runs", {"skill": "rag", "request": {"question": "  "}})[0], 400)
        self.assertEqual(self.json("/api/runs", {"skill": "rag", "request": {"question": "x", "top_k": 99}})[0], 400)
        self.assertEqual(self.json("/api/runs", {"skill": "rag", "request": {"question": "x", "provider": "openai"}})[0], 400)
        self.assertEqual(self.json("/api/runs", {"skill": "shorts", "request": {"question": "  "}})[0], 400)
        self.assertEqual(self.json("/api/runs", {"skill": "shorts", "request": {"question": "x", "mode": "tiktok"}})[0], 400)
        self.assertEqual(self.json("/api/runs", {"skill": "shorts", "request": {"question": "x", "limit": 99}})[0], 400)
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

    def test_feedback_on_a_finished_run(self):  # tick boxes (slow, wrong behavior, ...) saved on the run and in the log
        self.wait_idle()
        issues = {i["id"] for i in self.json("/api/feedback")[1]["issues"]}
        self.assertTrue({"slow", "wrong_behavior", "other"} <= issues)
        run_id = self.json("/api/runs", {"skill": "fake", "request": {"question": "rate me", "lines": 1}})[1]["run_id"]
        self.events(run_id)
        code, data = self.json(f"/api/runs/{run_id}/feedback", {"issues": ["wrong_behavior", "slow", "slow"], "comment": " איטי "})
        self.assertEqual(code, 200)
        self.assertEqual(data["feedback"]["rating"], "bad")  # a report without a rating is a 'bad' one
        self.assertEqual(data["feedback"]["issues"], ["slow", "wrong_behavior"])  # known order, no duplicates
        self.assertEqual(data["feedback"]["comment"], "איטי")
        self.assertEqual(self.json(f"/api/runs/{run_id}")[1]["summary"]["feedback"]["issues"], ["slow", "wrong_behavior"])
        report = self.json("/api/feedback")[1]["reports"][0]
        self.assertEqual((report["run_id"], report["question"], report["model"]), (run_id, "rate me", "fake"))
        self.assertEqual(self.json(f"/api/runs/{run_id}/feedback", {"rating": "good"})[1]["feedback"]["issues"], [])  # replaces
        self.assertEqual(next(r for r in self.json("/api/runs")[1]["runs"] if r["id"] == run_id)["feedback"]["rating"], "good")
        saved = json.loads((Path(self.tmp.name) / f"{run_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["summary"]["feedback"]["rating"], "good")  # survives a restart
        manager = server.Handler.manager
        manager.runs.pop(run_id)  # as after a restart: the run comes from disk
        self.assertEqual(self.json(f"/api/runs/{run_id}/feedback", {"issues": ["incorrect"]})[0], 200)
        self.assertEqual(self.json(f"/api/runs/{run_id}")[1]["summary"]["feedback"]["issues"], ["incorrect"])
        for bad in ({"issues": ["nope"]}, {"rating": "meh"}, {}, {"rating": "good", "issues": ["slow"]},
                    {"comment": "x" * 1001}, ["slow"]):
            self.assertEqual(self.json(f"/api/runs/{run_id}/feedback", bad)[0], 400, bad)
        self.assertEqual(self.json("/api/runs/does-not-exist/feedback", {"rating": "good"})[0], 404)

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
        self.assertEqual(set(data["systems"]), {"ollama", "atlas", "anthropic", "gemini", "vault", "index", "voice", "tts"})
        text = raw.decode("utf-8")
        for marker in ("mongodb+srv://", "mongodb://", "sk-ant-", "password", ".mongodb.net"):
            self.assertNotIn(marker, text)

    def test_transcribe_rejects_empty_audio(self):
        code, raw, _ = self.request("/api/transcribe", body=None, method="POST")
        self.assertEqual(code, 400)

    def test_transcribe_rejects_garbage_audio(self):  # not a media file: a clear 400, before any model loads
        code, raw, _ = self.request("/api/transcribe", body=b"not audio at all" * 10, method="POST")
        self.assertEqual(code, 400)

    def test_voice_audio_preparation(self):  # decode from memory, then level normalization; no model needed
        import io, wave
        import numpy as np
        import voice
        tone = (0.05 * np.sin(np.linspace(0, 2 * np.pi * 440, 16000))).astype(np.float32)  # quiet 1 s tone
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes((tone * 32767).astype("<i2").tobytes())
        samples = voice.decode(buffer.getvalue())
        self.assertAlmostEqual(len(samples) / voice.SAMPLE_RATE, 1.0, delta=0.05)
        louder = voice.normalize(samples)
        self.assertGreater(np.abs(louder).max(), 0.5)  # quiet speech is brought up
        self.assertLessEqual(np.abs(louder).max(), 1.0)
        self.assertEqual(np.abs(voice.normalize(np.zeros(1600, np.float32))).max(), 0)  # silence stays silence
        hiss = np.random.default_rng(0).normal(0, 1e-3, 16000).astype(np.float32)
        self.assertLessEqual(np.abs(voice.normalize(hiss)).max(), 1e-3 * 5 * voice.MAX_GAIN)  # gain is capped

    def test_voice_vocabulary_and_hallucinations(self):
        from types import SimpleNamespace as Seg
        import voice
        words = list(voice.identifiers("Errors in Cedar hit 94% (HTTP 401) per SYS-001. The p95 rose; see notification_delay."))
        for term in ("Cedar", "HTTP", "SYS-001", "p95", "notification_delay"):
            self.assertIn(term, words)
        self.assertNotIn("The", words)  # a sentence start is not a name
        segment = lambda text, nsp=0.5, lp=-0.8: Seg(text=text, no_speech_prob=nsp, avg_logprob=lp, start=0, end=1)
        kept = voice.clean([segment(" Thanks for watching!"), segment("תודה רבה."), segment("כתוביות: עמית", 0, -0.1),
                            segment("מה קרה ב־Cedar?", 0.01, -0.2), segment("  ", 0, 0)])
        self.assertEqual([text for _, text in kept], ["מה קרה ב־Cedar?"])
        self.assertEqual(len(voice.clean([segment("תודה רבה", 0.01, -0.1)])), 1)  # said clearly, it's real speech

    def test_speak_rejects_empty_text(self):  # validated before any model loads
        code, raw, _ = self.request("/api/speak", body={"text": "  "}, method="POST")
        self.assertEqual(code, 400)
        code, raw, _ = self.request("/api/speak", body={"text": "hello", "language": "xx"}, method="POST")
        self.assertEqual(code, 400)

    def test_speech_streams_neural_voice_and_falls_back_to_local(self):  # no network: the engines are stubbed
        import tts
        saved = tts.edge_ready, tts.edge_stream, tts.synthesize
        def neural(text, language=None):
            yield b"ID3"
            yield b"-mp3"
        def offline(text, language=None):
            raise ConnectionError("offline")
            yield b""
        try:
            tts.edge_ready = lambda: True
            tts.edge_stream = neural
            code, data = self.json("/api/speak/prepare", {"text": "שלום, זה Jarvis", "language": "he"})
            self.assertEqual((code, data["engine"]), (200, "edge"))
            code, body, headers = self.request(f"/api/speak/{data['id']}")
            self.assertEqual((code, headers["Content-Type"], body), (200, "audio/mpeg", b"ID3-mp3"))
            self.assertEqual(self.request(f"/api/speak/{data['id']}")[1], b"ID3-mp3")  # the browser may fetch twice
            tts.edge_stream = offline
            tts.synthesize = lambda text, language=None: (b"RIFF-wav", {"language": "he"})
            code, body, headers = self.request(f"/api/speak/{data['id']}")
            self.assertEqual((code, headers["Content-Type"], body), (200, "audio/wav", b"RIFF-wav"))
            self.assertEqual(self.request("/api/speak/unknown")[0], 404)
        finally:
            tts.edge_ready, tts.edge_stream, tts.synthesize = saved
        self.assertEqual(tts.prepare_neural("**שגיאה** 401 ב-Auth [c-2] https://x.y"), "שגיאה 401 ב Auth")

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


JUDGED_SOURCES = [{"chunk_id": "c1", "text": "Escalate auth errors to the on-call SRE after 30 minutes.",
                   "metadata": {"document_id": "auth-runbook", "version": 2, "status": "current"}},
                  {"chunk_id": "c2", "text": "Escalate auth errors to the team lead.",
                   "metadata": {"document_id": "auth-runbook", "version": 1, "status": "superseded"}}]
JUDGED_ANSWER_EVENTS = [
    {"seq": 0, "type": "run_start", "skill": "jarvis"},
    {"seq": 1, "type": "route", "skill": "rag", "title": "Ask the documents", "reason": "a question about the notes"},
    {"seq": 2, "type": "answer", "question": "who handles auth errors?", "missing_information": [], "sources": JUDGED_SOURCES,
     "claims": [{"text": "Auth errors go to the on-call SRE.", "citation_ids": ["c1"]},
                {"text": "Escalate after 30 minutes.", "citation_ids": ["c9"]}],
     "citation_status": "invalid_source_ids", "invalid_source_ids": ["c9"], "provider": "ollama", "model": "gemma3:4b", "seconds": 1},
    {"seq": 3, "type": "run_end", "status": "done"}]
JUDGE_REPLY = {"claims": [{"support": "supported", "cited_ok": True, "reason": "S1 says so"},
                          {"support": "partial", "cited_ok": True, "reason": "the time is in S1"}],
               "tool_choice": {"score": 5, "reason": "a notes question went to the notes"},
               "relevance": {"score": 4, "reason": "answers who"}, "completeness": {"score": 7, "reason": "all of it"},
               "current_version": {"score": 5, "reason": "uses v2"}, "language": {"score": 4, "reason": "clear"}}


class Judge(unittest.TestCase):  # judge.py: what is judged, which criteria apply, the prompt, the schema, the scores
    def setUp(self):
        import judge
        self.judge = judge

    def test_criteria_follow_what_the_run_did(self):
        judge = self.judge
        case = judge.case_from_run(JUDGED_ANSWER_EVENTS, {"question": "who handles auth errors?"})
        self.assertEqual(judge.applicable(case), ["tool_choice", "faithfulness", "citations", "relevance", "completeness",
                                                  "current_version", "language"])
        current_only = [{**e, "sources": JUDGED_SOURCES[:1]} if e["type"] == "answer" else e for e in JUDGED_ANSWER_EVENTS]
        self.assertNotIn("current_version", judge.applicable(judge.case_from_run(current_only, {"question": "q"})))
        direct = [e for e in JUDGED_ANSWER_EVENTS if e["type"] != "route"]  # the user picked "Ask the documents": no tool decision
        self.assertNotIn("tool_choice", judge.applicable(judge.case_from_run(direct, {"question": "q"})))
        shorts = [{"type": "route", "skill": "shorts", "title": "YouTube Shorts", "reason": "a YouTube request"},
                  {"type": "log", "stage": "mcp", "line": 'MCP: call search_youtube_shorts {"query": "cats", "limit": 5}'},
                  {"type": "posts", "tool": "search_youtube_shorts", "query": "cats", "posts": [{"title": "Cat fails"}]}]
        case = judge.case_from_run(shorts, {"question": "find shorts about cats"})
        self.assertEqual(judge.applicable(case), ["tool_choice", "relevance"])
        self.assertIn('MCP: call search_youtube_shorts {"query": "cats"', judge.prompt(case, judge.applicable(case)))
        self.assertIn("1. Cat fails", judge.prompt(case, judge.applicable(case)))
        meme = [{"type": "meme", "template": "drake", "name": "Drakeposting", "text": ["a", "b"], "notes": []}]
        self.assertEqual(judge.applicable(judge.case_from_run(meme, {"question": "drake meme: a / b"})), ["relevance", "language"])
        self.assertIsNone(judge.case_from_run([{"type": "message", "text": "All systems are ready."}], {}))

    def test_prompt_and_schema(self):
        judge = self.judge
        case = judge.case_from_run(JUDGED_ANSWER_EVENTS, {"question": "מי מטפל בשגיאות התחברות?"})
        ids = judge.applicable(case)
        text = judge.prompt(case, ids)
        for part in ("<<<SOURCES", "[S1] (document_id=auth-runbook, version=2, status=current)", "[S2]", "SOURCES>>>",
                     "Claim 2: Escalate after 30 minutes.   [cites: c9 (NOT among the sources sent)]", "exactly 2 items",
                     "(Jarvis answered it as: who handles auth errors?)", "in Hebrew"):
            self.assertIn(part, text)
        self.assertLess(text.index("<<<SOURCES"), text.index("Escalate auth errors to the team lead."), )
        self.assertLess(text.index("Escalate auth errors to the team lead."), text.index("SOURCES>>>"))  # notes stay inside the data block
        properties = judge.schema(case, ids)["properties"]
        self.assertEqual(set(properties), {"claims", "tool_choice", "relevance", "completeness", "current_version", "language"})
        self.assertEqual(properties["claims"]["items"]["properties"]["support"]["enum"], ["supported", "partial", "unsupported"])

    def test_scores_are_computed_not_written_by_the_model(self):
        judge = self.judge
        case = judge.case_from_run(JUDGED_ANSWER_EVENTS, {"question": "who handles auth errors?"})
        ids = judge.applicable(case)
        result = judge.score(case, ids, JUDGE_REPLY)
        rows = {r["id"]: r for r in result["criteria"]}
        self.assertEqual(rows["faithfulness"]["score"], 4.0)  # supported + partial = 0.75 -> 1 + 4 * 0.75
        self.assertEqual(rows["citations"]["score"], 3.0)  # claim 2 cites c9, never sent: wrong whatever the model says
        self.assertEqual([c["cited_ok"] for c in result["claims"]], [True, False])
        self.assertEqual(rows["completeness"]["score"], 5)  # 7 is clamped to 1..5
        self.assertEqual(result["overall"], 4.3)  # (5 + 4 + 3 + 4 + 5 + 5 + 4) / 7 = 4.29
        empty = judge.score(case, ids, {"claims": "junk", "relevance": {"score": "high"}})
        self.assertIsNone(empty["overall"])  # a broken reply gives no verdict, never a made-up score
        self.assertEqual([c["support"] for c in empty["claims"]], [None, None])
        no_versions = judge.case_from_run([{**e, "sources": JUDGED_SOURCES[:1]} if e["type"] == "answer" else e
                                           for e in JUDGED_ANSWER_EVENTS], {"question": "q"})
        row = next(r for r in judge.score(no_versions, judge.applicable(no_versions), {})["criteria"] if r["id"] == "current_version")
        self.assertEqual((row["score"], row["reason"]), (None, "No older versions among the sources"))  # shown as n/a

    def test_judge_uses_the_model_that_answered(self):
        pick, judge = skills.JudgeSkill.judge_model, self.judge
        answered = judge.case_from_run(JUDGED_ANSWER_EVENTS, {"question": "q"})
        self.assertEqual(pick(answered, {"provider": "anthropic"}), ("ollama", "gemma3:4b"))
        shorts = judge.case_from_run([{"type": "posts", "tool": "t", "query": "q", "posts": []}], {"question": "q"})
        self.assertEqual(pick(shorts, {"provider": "anthropic", "model": ""}), ("anthropic", skills.rag.DEFAULT_MODELS["anthropic"]))
        self.assertEqual(pick(shorts, {"provider": "ollama", "model": "qwen3:8b"}), ("ollama", "qwen3:8b"))
        meme = judge.case_from_run([{"type": "meme", "template": "drake", "name": "Drake", "text": ["a", "b"]}], {"question": "q"})
        self.assertEqual(pick(meme, {"provider": "anthropic", "route": {"request": {"captions_by": "ollama"}}}),
                         ("ollama", "gemma3:4b"))  # Ask Jarvis -> meme: the captions model, not "Answer with"
        self.assertEqual(pick(meme, {"captions_by": "gemini"}), ("gemini", skills.rag.DEFAULT_MODELS["gemini"]))  # a direct meme run
        import urllib.error
        rejected = urllib.error.HTTPError("https://api.anthropic.com/v1/messages", 401, "Unauthorized", {}, None)
        self.assertEqual(skills.JudgeSkill.failure(rejected, "anthropic"),
                         "anthropic rejected the API key (HTTP 401). Put a valid key in your .env / atlas-credentials.env")


FAKE_MCP_SERVER = r'''
# A tiny stdio MCP server (stdlib only) for the mcp_client tests: one JSON-RPC message per line.
import json, os, sys, threading, time
state = {"initialized": False, "cancelled": []}
out = threading.Lock()
TOOLS = [{"name": name, "description": f"Fake {name} tool.", "inputSchema": {"type": "object", "properties": {}}}
         for name in ("echo", "fail", "sleep", "exit", "cancelled", "search_youtube_shorts")]
TOOLS.append({"name": "wrapped", "description": "A list result, wrapped like fastmcp does.", "inputSchema": {"type": "object"},
              "outputSchema": {"type": "object", "properties": {"result": {}}, "x-fastmcp-wrap-result": True}})

def send(message):
    with out:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

def data(request_id, value):
    send({"jsonrpc": "2.0", "id": request_id,
          "result": {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}})

def call(request_id, name, args):
    print(f"fake: {name} called", file=sys.stderr, flush=True)
    if name == "fail":
        send({"jsonrpc": "2.0", "id": request_id,
              "result": {"isError": True, "content": [{"type": "text", "text": "Error calling tool 'fail': boom"}]}})
    elif name == "exit":
        print("fake: crashing", file=sys.stderr, flush=True)
        os._exit(3)
    elif name == "sleep":
        time.sleep(args.get("seconds", 5))
        data(request_id, {"ok": True, "slept": True})
    elif name == "cancelled":
        data(request_id, {"ok": True, "ids": state["cancelled"]})
    elif name == "wrapped":
        data(request_id, {"result": [1, 2]})
    elif name == "search_youtube_shorts":
        data(request_id, {"ok": True, "count": 1, "posts": [{"title": f"{args['query']} short", "url": "https://youtube.com/shorts/x"}]})
    else:
        data(request_id, {"ok": True, "pid": os.getpid(), "arguments": args, "initialized": state["initialized"]})

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        print("fake: starting", file=sys.stderr, flush=True)
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": message["params"]["protocolVersion"],
              "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "notifications/initialized":
        state["initialized"] = True
    elif method == "notifications/cancelled":
        state["cancelled"].append(message["params"]["requestId"])
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = message["params"]
        threading.Thread(target=call, args=(message["id"], params["name"], params.get("arguments") or {}), daemon=True).start()
'''


class McpClient(unittest.TestCase):  # mcp_client.py against the fake server above: real processes and pipes, no network
    @classmethod
    def setUpClass(cls):
        import mcp_client
        cls.mcp = mcp_client
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "fake").mkdir()
        (cls.root / "fake" / "server.py").write_text(FAKE_MCP_SERVER, encoding="utf-8")
        cls.spec = {"type": "stdio", "command": "python", "args": ["fake/server.py"]}  # relative to root, like .mcp.json

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def client(self, *names):
        client = self.mcp.Client({name: self.spec for name in names or ("fake",)}, root=self.root)
        self.addCleanup(client.close)
        return client

    def test_call_tool_and_list_tools(self):
        client, lines = self.client(), []
        self.assertEqual(client.servers(), ["fake"])
        tools = client.list_tools("fake")
        self.assertIn({"name": "echo", "description": "Fake echo tool.", "input_schema": {"type": "object", "properties": {}}}, tools)
        self.assertEqual(set(tools[0]), {"name", "description", "input_schema"})
        data = client.call_tool("fake", "echo", {"text": "שלום"}, log=lines.append)
        self.assertEqual((data["arguments"], data["initialized"]), ({"text": "שלום"}, True))  # UTF-8 both ways; handshake done
        self.assertEqual(client.call_tool("fake", "wrapped", {}), [1, 2])  # fastmcp's {"result": ...} is unwrapped
        self.assertIn('MCP: call echo {"text": "שלום"}', lines)
        deadline = time.time() + 5
        while "fake: echo called" not in lines and time.time() < deadline:  # the server's stderr, forwarded
            time.sleep(0.02)
        self.assertIn("fake: echo called", lines)
        with self.assertRaisesRegex(self.mcp.McpError, "^boom$"):  # the server's message, without fastmcp's prefix
            client.call_tool("fake", "fail", {})
        with self.assertRaisesRegex(self.mcp.McpError, "not offered"):
            client.call_tool("fake", "nope", {})
        with self.assertRaisesRegex(self.mcp.McpError, "Unknown MCP server"):
            client.call_tool("other", "echo", {})

    def test_one_process_serves_every_call(self):
        client, lines = self.client(), []
        first = client.call_tool("fake", "echo", {}, log=lines.append)["pid"]
        second = client.call_tool("fake", "echo", {}, log=lines.append)["pid"]
        client.list_tools("fake")
        self.assertEqual(first, second)
        self.assertEqual(sum(line.startswith("MCP: connected to fake") for line in lines), 1)
        threads = [threading.Thread(target=lambda: client.call_tool("fake", "echo", {})) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(client.call_tool("fake", "echo", {})["pid"], first)  # thread-safe, still one process

    def test_reconnects_after_the_server_dies(self):
        client, lines = self.client(), []
        first = client.call_tool("fake", "echo", {})["pid"]
        client.connections["fake"].process.kill()  # died between calls
        client.connections["fake"].process.wait(5)
        second = client.call_tool("fake", "echo", {}, log=lines.append)["pid"]
        self.assertNotEqual(first, second)
        self.assertTrue(any("starting it again" in line for line in lines))
        with self.assertRaisesRegex(self.mcp.McpError, "stopped during the call"):  # dies mid-call: one retry, then a clear error
            client.call_tool("fake", "exit", {}, log=lines.append)
        self.assertEqual(sum(line == "MCP: call exit {}" for line in lines), 2)
        self.assertNotEqual(client.call_tool("fake", "echo", {})["pid"], second)  # and the next call works again

    def test_timeout_restarts_a_stuck_server(self):
        client = self.client()
        first = client.call_tool("fake", "echo", {})["pid"]
        started = time.time()
        with self.assertRaisesRegex(self.mcp.McpError, r"didn't answer sleep within 0\.5 s"):
            client.call_tool("fake", "sleep", {"seconds": 30}, timeout=0.5)
        self.assertLess(time.time() - started, 5)
        self.assertNotEqual(client.call_tool("fake", "echo", {})["pid"], first)

    def test_cancel_stops_waiting_and_tells_the_server(self):
        client, stop = self.client(), threading.Event()
        first = client.call_tool("fake", "echo", {})["pid"]
        threading.Timer(0.3, stop.set).start()
        started = time.time()
        with self.assertRaises(self.mcp.Cancelled):
            client.call_tool("fake", "sleep", {"seconds": 30}, cancelled=stop.is_set)
        self.assertLess(time.time() - started, 3)
        self.assertEqual(len(client.call_tool("fake", "cancelled", {})["ids"]), 1)  # notifications/cancelled arrived
        self.assertEqual(client.call_tool("fake", "echo", {})["pid"], first)  # the connection is kept

    def test_server_that_cannot_start(self):
        client = self.mcp.Client({"broken": {"command": "python", "args": ["fake/missing.py"]}}, root=self.root)
        self.addCleanup(client.close)
        with self.assertRaisesRegex(self.mcp.McpError, "The broken MCP server didn't start: .*missing"):
            client.list_tools("broken")

    def test_servers_come_from_mcp_json_with_their_venv(self):
        self.assertEqual(self.mcp.servers(), ["youtube-shorts", "memes"])  # jarvis/.mcp.json
        argv, cwd, _ = self.mcp.resolve(self.spec, self.root)
        self.assertEqual((argv, Path(cwd)), ([sys.executable, str(self.root / "fake" / "server.py")], self.root / "fake"))
        venv = self.root / "fake" / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        venv.parent.mkdir(parents=True)
        venv.write_text("")
        try:
            self.assertEqual(self.mcp.resolve(self.spec, self.root)[0][0], str(venv))  # the server's own venv first
        finally:
            venv.unlink()
        config = self.root / ".mcp.json"
        config.write_text(json.dumps({"mcpServers": {"b": self.spec, "a": self.spec, "web": {"type": "http", "url": "x"}}}))
        self.assertEqual(self.mcp.Client(config).servers(), ["b", "a"])  # stdio servers, in file order

    def test_skills_call_tools_over_the_shared_connection(self):  # ShortsSkill / MemeSkill -> mcp_client -> fake server
        import runs
        saved, self.mcp.DEFAULT = self.mcp.DEFAULT, self.client("youtube-shorts", "memes")
        try:
            request = skills.SKILLS["shorts"].validate({"question": "cats", "limit": 2})
            run = runs.Run("shorts", request)
            skills.SKILLS["shorts"].run(request, run)
            posts = next(e for e in run.events if e["type"] == "posts")
            self.assertEqual((posts["tool"], posts["posts"][0]["title"]), ("search_youtube_shorts", "cats short"))
            logs = [e["line"] for e in run.events if e["type"] == "log" and e["stage"] == "mcp"]
            self.assertIn('MCP: call search_youtube_shorts {"query": "cats", "limit": 2, "details": false}', logs)
            self.assertEqual(next(e for e in run.events if e["type"] == "stage_end")["status"], "ok")
            with self.assertRaisesRegex(RuntimeError, "^Memes MCP call failed — boom$"):
                skills.SKILLS["meme"].call(run, "plan", "fail", {})
            run = runs.Run("meme", {})
            run.cancelled = True
            with self.assertRaises(runs.Cancelled):
                skills.SKILLS["meme"].call(run, "mcp", "sleep", {"seconds": 30})
        finally:
            self.mcp.DEFAULT = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
