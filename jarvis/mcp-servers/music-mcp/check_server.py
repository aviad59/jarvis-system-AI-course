"""Tests for the music MCP server. Run: python mcp-servers/music-mcp/check_server.py

No network: a fake supplier (a FastMCP server with Suno-like tools) is reached in memory, and our own server is
called through a real MCP client, the way Jarvis and Claude Code call it.
"""
import asyncio
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastmcp import Client, FastMCP  # noqa: E402

import mapping  # noqa: E402
import normalize  # noqa: E402
import server  # noqa: E402
import upstream  # noqa: E402

FAKE_ENTRY = {"key": "fake", "name": "FakeTunes", "url": "https://fake.example/mcp", "auth": "none", "token": "",
              "ops": {}, "site": ""}


def fake_supplier(polls_until_ready=2, fail=False):
    """Suno-like tools: generate_music starts two clips; get_music_status reports them ready after a few polls."""
    supplier, state = FastMCP("fake-suno"), {"polls": 0, "calls": []}

    @supplier.tool(description="Generate music from a prompt. Returns two clips (song variations).")
    def generate_music(prompt: str, tags: str = "", make_instrumental: bool = False) -> dict:
        state["calls"].append(("generate_music", {"prompt": prompt, "tags": tags, "make_instrumental": make_instrumental}))
        if fail:
            raise ValueError("out of credits")
        return {"task_id": "t-1", "clips": [{"id": "c1", "status": "submitted", "title": ""},
                                           {"id": "c2", "status": "queued", "title": ""}]}

    @supplier.tool(description="Check the status of clips by id.")
    def get_music_status(ids: list[str]) -> dict:
        state["polls"] += 1
        state["calls"].append(("get_music_status", {"ids": ids}))
        done = state["polls"] >= polls_until_ready * 2  # one poll per waiting clip
        return {"data": [{"id": i, "title": "Monday Blues", "status": "complete" if done else "streaming",
                          "audioUrl": f"https://cdn.fake.example/{i}.mp3" if done else None,
                          "image_url": f"https://cdn.fake.example/{i}.jpg", "metadata": {"duration": 121.37, "tags": "lofi"}}
                         for i in ids]}

    @supplier.tool(description="List your recent music in the library.")
    def list_recent_music(limit: int = 10) -> list:
        return [{"id": f"r{i}", "title": f"Song {i}", "status": "complete", "audio_url": f"https://cdn.fake.example/r{i}.mp3"}
                for i in range(limit)]

    @supplier.tool(description="Get your remaining credits.")
    def get_credits() -> dict:
        return {"credits_left": 42, "monthly_limit": 500}

    @supplier.tool(description="Generate a sound effect audio clip.")
    def generate_sfx(prompt: str) -> dict:
        return {}

    return supplier, state


class Supplied(unittest.TestCase):  # our server against the fake supplier, through real MCP clients
    def setUp(self):
        self.saved = server.UPSTREAM, server.POLL, server.WAIT
        server.POLL, server.WAIT = 0.01, 5

    def tearDown(self):
        server.UPSTREAM, server.POLL, server.WAIT = self.saved

    def use(self, supplier, ops=None):
        server.UPSTREAM = upstream.Upstream(entry={**FAKE_ENTRY, "ops": ops or {}}, connect=lambda: Client(supplier))

    def call(self, tool, **arguments):
        async def go():
            async with Client(server.mcp) as client:
                return (await client.call_tool(tool, arguments)).structured_content
        return asyncio.run(go())

    def test_create_song_waits_until_the_songs_are_ready(self):
        supplier, state = fake_supplier()
        self.use(supplier)
        result = self.call("create_song", description="monday meetings", style="lo-fi", instrumental=True)
        self.assertEqual((result["ok"], result["status"], result["supplier"], result["tool"]), (True, "ready", "FakeTunes", "generate_music"))
        self.assertEqual([s["id"] for s in result["songs"]], ["c1", "c2"])
        first = result["songs"][0]
        self.assertEqual((first["audio_url"], first["title"], first["duration"], first["style"]),
                         ("https://cdn.fake.example/c1.mp3", "Monday Blues", 121.4, "lofi"))
        self.assertEqual(state["calls"][0], ("generate_music", {"prompt": "monday meetings", "tags": "lo-fi", "make_instrumental": True}))
        self.assertEqual(state["calls"][1], ("get_music_status", {"ids": ["c1", "c2"]}))  # all at once: the schema takes a list

    def test_suno_answer_with_advice_after_the_json(self):  # audio-create: JSON, then "Call audio-wait once with all of them"
        supplier, state = fake_supplier()

        @supplier.tool(name="audio-create", description="Create audio from a description.")
        def audio_create(description: str) -> str:
            state["calls"].append(("audio-create", {"description": description}))
            return ('{"clip_ids":["c1","c2"],"created_at":"2026-11-26T12:30:59Z","id":"c1","status":"submitted"} '
                    "This made 2 songs, one per id: c1, c2. Call audio-wait once with all of them in ids, then tell the "
                    "user about every song, not only the first.")
        self.use(supplier, {"create_song": "audio-create", "get_song": {"tool": "get_music_status", "args": {"song_id": "ids"}}})
        result = self.call("create_song", description="monday meetings")
        self.assertEqual((result["status"], [s["id"] for s in result["songs"]]), ("ready", ["c1", "c2"]))
        self.assertEqual([s["audio_url"] for s in result["songs"]], ["https://cdn.fake.example/c1.mp3", "https://cdn.fake.example/c2.mp3"])
        self.assertEqual({c[1]["ids"] == ["c1", "c2"] for c in state["calls"][1:]}, {True})  # one call, every id

    def test_get_song_with_several_ids(self):
        supplier, state = fake_supplier(polls_until_ready=0)
        self.use(supplier)
        result = self.call("get_song", song_ids=["c1", "c2", "c1"])
        self.assertEqual((result["status"], [s["id"] for s in result["songs"]]), ("ready", ["c1", "c2"]))
        self.assertEqual(state["calls"], [("get_music_status", {"ids": ["c1", "c2"]})])
        self.assertIn("Give a song id", self.call("get_song")["error"])

    def test_create_song_without_waiting_and_on_timeout(self):
        supplier, _ = fake_supplier(polls_until_ready=10 ** 6)
        self.use(supplier)
        quick = self.call("create_song", description="x", wait=False)
        self.assertEqual((quick["status"], [s["ready"] for s in quick["songs"]]), ("pending", [False, False]))
        server.WAIT = 0.2
        slow = self.call("create_song", description="x")
        self.assertEqual(slow["status"], "pending")
        self.assertIn("get_song", slow["message"])

    def test_the_other_tools(self):
        supplier, _ = fake_supplier(polls_until_ready=0)
        self.use(supplier)
        self.assertEqual(self.call("get_song", song_id="c9")["songs"][0]["audio_url"], "https://cdn.fake.example/c9.mp3")
        listed = self.call("list_songs", limit=3)
        self.assertEqual((listed["count"], listed["tool"]), (3, "list_recent_music"))
        credits = self.call("get_credits")
        self.assertEqual((credits["credits"], credits["tool"]), (42, "get_credits"))
        tools = self.call("supplier_tools")
        self.assertEqual(tools["uses"], {"create_song": "generate_music", "get_credits": "get_credits",
                                         "list_songs": "list_recent_music", "get_song": "get_music_status"})

    def test_errors_are_messages_not_tracebacks(self):
        logging.getLogger("fastmcp").setLevel(logging.CRITICAL)  # the fake supplier's own traceback: expected here
        supplier, _ = fake_supplier(fail=True)
        self.use(supplier)
        failed = self.call("create_song", description="x")
        self.assertFalse(failed["ok"])
        self.assertIn("FakeTunes (generate_music)", failed["error"])
        self.assertIn("out of credits", failed["error"])
        self.assertIn("description", self.call("create_song")["error"])  # nothing to make a song of
        self.assertIn("Invalid song id", self.call("get_song", song_id="../../etc")["error"])
        self.use(supplier, ops={"create_song": "no_such_tool"})
        self.assertIn("maps create_song to 'no_such_tool'", self.call("create_song", description="x")["error"])

    def test_not_signed_in_never_opens_a_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            saved = os.environ.get("MUSIC_TOKEN_DIR")
            os.environ["MUSIC_TOKEN_DIR"] = folder
            try:
                server.UPSTREAM = upstream.Upstream(entry={**FAKE_ENTRY, "auth": "oauth"})  # a real client, no token
                result = self.call("create_song", description="x")
                self.assertFalse(result["ok"])
                self.assertIn("Not signed in to FakeTunes", result["error"])
                self.assertIn("login.py", result["error"])
                self.assertFalse(asyncio.run(upstream.signed_in(server.UPSTREAM.entry)))
                asyncio.run(self.store_token(server.UPSTREAM.entry))
                self.assertTrue(asyncio.run(upstream.signed_in(server.UPSTREAM.entry)))  # survives a new store
                asyncio.run(upstream.sign_out(server.UPSTREAM.entry))
                self.assertFalse(asyncio.run(upstream.signed_in(server.UPSTREAM.entry)))
            finally:
                if saved is None:
                    os.environ.pop("MUSIC_TOKEN_DIR", None)
                else:
                    os.environ["MUSIC_TOKEN_DIR"] = saved

    @staticmethod
    async def store_token(entry):
        from mcp.shared.auth import OAuthToken
        await upstream._tokens(entry).set_tokens(OAuthToken(access_token="secret", token_type="Bearer", expires_in=3600))


class Mapping(unittest.TestCase):
    TOOLS = [{"name": n, "description": d} for n, d in (
        ("generate_music", "Generate a song from a prompt"), ("wait_for_music", "Wait until the two variations are ready"),
        ("get_music_status", "Status of a music task"), ("list_recent_music", "Your recent songs"),
        ("search_library", "Search your library"), ("generate_sfx", "Generate a sound effect audio clip"),
        ("get_usage", "Usage this month"), ("get_referral_link", "Your referral link"), ("get_credits", "Credits left"),
        ("generate_lyrics", "Write lyrics for a song"))]

    def test_tools_are_matched_by_name(self):
        chosen = mapping.auto_map(self.TOOLS)
        self.assertEqual(chosen["create_song"], "generate_music")  # not generate_sfx / generate_lyrics
        self.assertEqual(chosen["get_credits"], "get_credits")
        self.assertEqual(chosen["list_songs"], "list_recent_music")
        self.assertIn(chosen["get_song"], ("get_music_status", "wait_for_music"))
        camel = mapping.auto_map([{"name": "createSong", "description": ""}, {"name": "getSongStatus", "description": ""}])
        self.assertEqual((camel["create_song"], camel["get_song"], camel["list_songs"]), ("createSong", "getSongStatus", None))

    def test_explicit_mapping_wins(self):
        plan = mapping.plan("create_song", self.TOOLS, {"create_song": {"tool": "generate_sfx", "fixed": {"model": "v5"}}})
        self.assertEqual((plan["tool"]["name"], plan["fixed"]), ("generate_sfx", {"model": "v5"}))
        self.assertEqual(mapping.plan("get_song", self.TOOLS, {"get_song": "wait_for_music"})["tool"]["name"], "wait_for_music")
        with self.assertRaisesRegex(LookupError, "no tool for create_song"):
            mapping.plan("create_song", [{"name": "ping", "description": ""}], {})

    def test_arguments_are_matched_and_folded(self):
        schema = {"properties": {"prompt": {"type": "string"}, "tags": {"type": "string"}, "ids": {"type": "array"}}}
        args, dropped = mapping.build_args({"description": "cats", "style": "jazz", "title": "Meow", "instrumental": True}, schema)
        self.assertEqual(args, {"prompt": "cats Title: Meow. Instrumental, no vocals.", "tags": "jazz"})  # no field: in the text
        self.assertEqual(dropped, [])
        self.assertEqual(mapping.build_args({"song_id": "c1"}, schema)[0], {"ids": ["c1"]})
        args, _ = mapping.build_args({"description": "cats", "style": "jazz"}, schema, explicit={"style": None}, fixed={"mv": "v5"})
        self.assertEqual(args, {"prompt": "cats", "mv": "v5"})  # mapped to null: never sent
        odd = {"properties": {"brief": {"type": "string"}, "n": {"type": "integer"}}, "required": ["brief"]}
        self.assertEqual(mapping.build_args({"description": "rain", "limit": "3"}, odd)[0], {"brief": "rain", "n": 3})
        self.assertEqual(mapping.build_args({"description": "rain"}, {})[0], {"description": "rain"})  # no schema: ours
        self.assertEqual(mapping.build_args({"description": "x", "instrumental": False}, schema)[0], {"prompt": "x"})

    def test_suno_like_tools(self):  # Suno's real names: "audio-listen" asks about a song, it isn't a list
        tools = [{"name": n, "description": ""} for n in ("audio-create", "audio-listen", "audio-wait", "library-list",
                                                          "account-usage", "audio-covers-create")]
        self.assertEqual(mapping.auto_map(tools), {"create_song": "audio-create", "get_credits": "account-usage",
                                                   "list_songs": "library-list", "get_song": "audio-wait"})

    def test_description_or_lyrics(self):  # Suno: a description alone, or lyrics with style and title; both = HTTP 400
        schema = {"properties": {k: {"type": "string"} for k in ("description", "lyrics", "style", "title")}}
        song = {"description": "cats", "lyrics": "[Verse]\nmeow", "style": "jazz", "title": "Meow"}
        plan = mapping.plan("create_song", [{"name": "audio-create", "description": ""}],
                            {"create_song": {"tool": "audio-create", "description_or_lyrics": True}})
        self.assertTrue(plan["modes"])
        self.assertEqual(mapping.build_args(song, schema, modes=True)[0],
                         {"lyrics": "[Verse]\nmeow", "style": "jazz", "title": "Meow"})
        self.assertEqual(mapping.build_args({"description": "cats", "style": "jazz", "title": "Meow"}, schema, modes=True)[0],
                         {"description": "cats Style: jazz. Title: Meow."})
        self.assertEqual(mapping.build_args(song, schema)[0], song)  # without the mode: everything by name


class Normalize(unittest.TestCase):
    def test_clip_ids_and_song_url(self):  # Suno: creation answers clip ids; a clip plays at its song_url
        pending = normalize.songs({"clip_ids": ["c1", "c2"], "status": "submitted"})
        self.assertEqual([(s["id"], s["status"], s["ready"]) for s in pending], [("c1", "pending", False), ("c2", "pending", False)])
        clip = normalize.songs({"clips": [{"id": "c1", "status": "streaming", "song_url": "https://cdn.example/c1"}]})[0]
        self.assertEqual((clip["audio_url"], clip["ready"]), ("https://cdn.example/c1", True))

    def test_songs_in_any_shape(self):
        data = {"result": {"songs": [{"clipId": "a", "status": "Complete", "streamAudioUrl": "https://x.example/a",
                                      "imageLargeUrl": "https://x.example/a.png", "title": "A"},
                                     {"id": "b", "status": "queued", "audio_url": "http://x.example/b.mp3"}]}}
        a, b = normalize.songs(*normalize.payload(data))
        self.assertEqual((a["id"], a["status"], a["ready"], a["audio_url"], a["image_url"]),
                         ("a", "complete", True, "https://x.example/a", "https://x.example/a.png"))
        self.assertEqual((b["audio_url"], b["ready"]), (None, False))  # http links are dropped
        url = normalize.songs([{"url": "https://x.example/s.mp3?sig=1", "name": "S"}])[0]
        self.assertEqual((url["audio_url"], url["page_url"]), ("https://x.example/s.mp3?sig=1", None))
        streaming = normalize.songs([{"id": "s", "status": "streaming", "audio_url": "https://x.example/s"}])[0]
        self.assertTrue(streaming["ready"])  # playable while it finishes
        self.assertTrue(normalize.failed({"status": "error"}))

    def test_text_results(self):
        data, _ = normalize.payload(None, ['{"clip_ids": ["a", "b"], "id": "a", "status": "submitted"} This made 2 songs.'])
        self.assertEqual([s["id"] for s in normalize.songs(data)], ["a", "b"])  # JSON with words after it
        self.assertIsNone(normalize.payload(None, ["Done {not json} here"])[0])
        data, text = normalize.payload(None, ['{"clips": [{"id": "z", "title": "Z", "status": "complete"}]}'])
        self.assertEqual(normalize.songs(data, text)[0]["id"], "z")
        data, text = normalize.payload(None, ["Done! Listen: https://cdn.example/song.mp3, or https://suno.com/song/1."])
        found = normalize.songs(data, text)
        self.assertEqual([s["audio_url"] for s in found], ["https://cdn.example/song.mp3"])
        self.assertEqual(normalize.credits(None, "You have 1,250 credits left"), 1250)
        self.assertEqual(normalize.credits({"account": {"totalCreditsLeft": 7}}), 7)
        self.assertEqual(normalize.task_id({"data": {"taskId": "abc"}}), "abc")
        self.assertEqual(normalize.songs(*normalize.payload(None, [], ["https://cdn.example/r.wav"]))[0]["audio_url"],
                         "https://cdn.example/r.wav")  # a resource link in the content


class Setup(unittest.TestCase):
    CONFIG = {"default": "suno", "suppliers": {"suno": {"name": "Suno", "url": "https://api.suno.com/mcp", "auth": "oauth"},
                                               "keyed": {"url": "https://music.example/mcp/", "auth": "bearer"}}}

    def test_supplier_from_config_and_environment(self):
        entry = upstream.supplier({}, self.CONFIG)
        self.assertEqual((entry["key"], entry["name"], entry["url"], entry["auth"]), ("suno", "Suno", "https://api.suno.com/mcp", "oauth"))
        keyed = upstream.supplier({"MUSIC_SUPPLIER": "keyed", "MUSIC_MCP_TOKEN": "k"}, self.CONFIG)
        self.assertEqual((keyed["url"], keyed["token"], keyed["name"]), ("https://music.example/mcp", "k", "keyed"))
        other = upstream.supplier({"MUSIC_MCP_URL": "https://other.example/mcp", "MUSIC_MCP_AUTH": "none"}, self.CONFIG)
        self.assertEqual((other["url"], other["auth"]), ("https://other.example/mcp", "none"))
        self.assertEqual(upstream.supplier({})["url"], "https://api.suno.com/mcp")  # the shipped suppliers.json

    def test_bad_setups_are_refused(self):
        for env in ({"MUSIC_SUPPLIER": "nope"}, {"MUSIC_MCP_URL": "http://api.suno.com/mcp"},
                    {"MUSIC_MCP_URL": "https://user:pw@api.suno.com/mcp"}, {"MUSIC_MCP_URL": "file:///etc/passwd"},
                    {"MUSIC_MCP_AUTH": "magic"}, {"MUSIC_SUPPLIER": "keyed"}):  # keyed without MUSIC_MCP_TOKEN
            with self.assertRaises(ValueError, msg=env):
                upstream.supplier(env, self.CONFIG)

    def test_inputs_are_bounded(self):
        ours = server.song_input("  a   song\nabout cats ", style="x" * 999, lyrics="la\nla")
        self.assertEqual((ours["description"], len(ours["style"]), ours["lyrics"]), ("a song about cats", 200, "la\nla"))
        self.assertEqual(server.song_input(lyrics="only lyrics")["description"], "")
        with self.assertRaises(ValueError):
            server.song_input()
        for bad in ("", "../x", "a b", "x" * 101, "id?x=1"):
            with self.assertRaises(ValueError, msg=bad):
                server.check_id(bad)
        self.assertEqual(server.check_id(" c1-2_3 "), "c1-2_3")

    def test_errors_explained(self):
        class Response:
            status_code = 403

        class HTTPStatusError(Exception):
            response = Response()
        group = ExceptionGroup("x", [RuntimeError("wrapped")])
        group.__cause__ = HTTPStatusError()
        error = upstream.explain(group, {**FAKE_ENTRY, "site": "https://fake.example/beta"})
        self.assertIn("refused the connection (HTTP 403)", str(error))
        self.assertIn("https://fake.example/beta", str(error))
        inner = upstream.NotSignedIn("sign in")
        self.assertIs(upstream.explain(ExceptionGroup("x", [inner]), FAKE_ENTRY), inner)


if __name__ == "__main__":
    unittest.main(verbosity=2)
