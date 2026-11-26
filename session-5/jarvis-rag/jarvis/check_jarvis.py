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
        self.assertEqual(set(data["systems"]), {"ollama", "atlas", "anthropic", "vault", "index", "voice", "tts"})
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
