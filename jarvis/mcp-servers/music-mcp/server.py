# MCP server for songs: a fixed set of tools in front of whichever music supplier suppliers.json names.
#
# The default supplier is Suno's official MCP server (beta, https://api.suno.com/mcp; sign in with your Suno account,
# songs use your Suno credits). It is a remote server with an OAuth sign-in, which Jarvis's stdio MCP client can't
# reach, and its tool names aren't published. So this server runs locally over stdio, signs in once (login.py), and
# maps our tools onto the supplier's (mapping.py: explicit names in suppliers.json, or matched by name).
# Switching supplier = a new entry in suppliers.json (or MUSIC_SUPPLIER / MUSIC_MCP_URL); Jarvis doesn't change.
#
# Safety: inputs are bounded and ids validated; only https suppliers and https links in results; the sign-in token
# stays in .tokens/ (git-ignored) and is never printed or logged; no argument logging; a tool call never opens a
# browser. What you describe is sent to the supplier, nothing else leaves this computer.
import asyncio
import os
import re
import time

from fastmcp import FastMCP

import mapping
import normalize
import upstream
from upstream import NotSignedIn, SupplierError

mcp = FastMCP("music")
UPSTREAM = upstream.Upstream()
LIMITS = {"description": 1000, "lyrics": 5000, "style": 200, "title": 100}
SONG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")


def _seconds(name, default, low, high):
    try:
        return max(low, min(float(os.environ.get(name) or default), high))
    except ValueError:
        return default


WAIT = _seconds("MUSIC_WAIT_SECONDS", 240, 0, 900)  # how long create_song(wait=True) waits for the songs
POLL = _seconds("MUSIC_POLL_SECONDS", 5, 0.01, 60)  # how often it asks


# ---------------------------------------------------------------- input checks
def _text(value, name) -> str:
    return " ".join(str(value or "").split())[:LIMITS[name]] if name != "lyrics" else str(value or "").strip()[:LIMITS[name]]


def song_input(description="", lyrics="", style="", title="", instrumental=False) -> dict:
    ours = {"description": _text(description, "description"), "lyrics": _text(lyrics, "lyrics"),
            "style": _text(style, "style"), "title": _text(title, "title"), "instrumental": bool(instrumental)}
    if not ours["description"] and not ours["lyrics"]:
        raise ValueError("Say what the song is about (description), or give its lyrics")
    return ours


def check_id(value) -> str:
    value = str(value or "").strip()
    if not SONG_ID.match(value):
        raise ValueError(f"Invalid song id {value[:60]!r}: use an id from create_song or list_songs")
    return value


# ---------------------------------------------------------------- calls to the supplier
async def _call(client, op, ours):
    """One supplier tool call for one of our ops -> (data, text, tool name). SupplierError if it fails."""
    entry = UPSTREAM.config()
    tools = await UPSTREAM.list_tools(client)
    try:
        chosen = mapping.plan(op, tools, entry["ops"])
    except LookupError as missing:
        raise SupplierError(f"{entry['name']}: {missing}") from None
    tool = chosen["tool"]
    args, _ = mapping.build_args(ours, tool["input_schema"], chosen["args"], chosen["fixed"], chosen.get("modes"))
    result = await client.call_tool(tool["name"], args, raise_on_error=False, timeout=120)
    texts = [getattr(c, "text", "") for c in result.content or [] if getattr(c, "type", "") == "text"]
    links = [str(getattr(c, "uri", "")) for c in result.content or [] if getattr(c, "type", "") == "resource_link"]
    data, text = normalize.payload(result.structured_content, texts, links)
    if result.is_error:
        raise SupplierError(f"{entry['name']} ({tool['name']}): {' '.join(text.split())[:300] or 'the tool failed'}")
    return data, text, tool["name"]


def _merge(songs, fresh):  # newer records replace older ones with the same id
    by_id = {s["id"]: s for s in fresh if s.get("id")}
    merged = [by_id.pop(s["id"], s) if s.get("id") else s for s in songs]
    return merged + [s for s in fresh if not s.get("id") or s["id"] in by_id]


async def _takes_many(client) -> bool:
    """True when the supplier's status tool takes a list of ids (Suno's audio-wait: "ids"), so one call asks for all."""
    entry = UPSTREAM.config()
    try:
        chosen = mapping.plan("get_song", await UPSTREAM.list_tools(client), entry["ops"])
    except LookupError:
        return False
    schema = chosen["tool"]["input_schema"]
    args, _ = mapping.build_args({"song_id": "x"}, schema, chosen["args"], chosen["fixed"])
    properties = (schema or {}).get("properties") or {}
    return any(value == ["x"] or ((properties.get(name) or {}).get("type") is None and name.lower().endswith("ids"))
               for name, value in args.items())


async def _ask(client, songs, ids, many):  # the latest records for these ids: one call with all of them, or one per id
    for batch in ([ids] if many else [[one] for one in ids]):
        data, text, _ = await _call(client, "get_song", {"song_id": batch if many else batch[0]})
        songs = _merge(songs, normalize.songs(data, text))
    return songs


async def _wait(client, songs, task, deadline):
    """Ask for the songs until every one is ready or failed, or the deadline passes. Returns the latest records."""
    many = await _takes_many(client)
    while time.monotonic() < deadline:
        waiting = [s["id"] for s in songs if s.get("id") and not s["ready"] and not normalize.failed(s)]
        ids = waiting or ([task] if task and not songs else [])
        if not ids:
            break
        await asyncio.sleep(min(POLL, max(0.0, deadline - time.monotonic())))
        try:
            songs = await _ask(client, songs, ids, many)
        except Exception:  # no status tool, a timeout or a hiccup: return what we have (the songs are in the library)
            break
    return songs


def _overall(songs) -> str:
    if songs and all(s["ready"] for s in songs if not normalize.failed(s)) and not all(normalize.failed(s) for s in songs):
        return "ready"
    if songs and all(normalize.failed(s) for s in songs):
        return "failed"
    return "pending"


async def _safely(work) -> dict:
    try:
        return await work()
    except (NotSignedIn, SupplierError, ValueError) as error:
        return {"ok": False, "error": str(error)}
    except Exception as error:  # never a traceback to the caller
        return {"ok": False, "error": f"{type(error).__name__}: {' '.join(str(error).split())[:300]}"}


# ---------------------------------------------------------------- tools
@mcp.tool(description="Make a song (music with vocals, or instrumental) from a description of what it's about, in any "
                      "language. Optional: your own lyrics, a style (genre, mood, instruments), a title. With wait=true "
                      "(default) it waits until the songs are ready (a few minutes at most) and returns audio links; "
                      "otherwise it returns the song ids at once (check them with get_song). Uses the supplier's credits.")
async def create_song(description: str = "", lyrics: str = "", style: str = "", title: str = "",
                      instrumental: bool = False, wait: bool = True) -> dict:
    async def work():
        ours = song_input(description, lyrics, style, title, instrumental)
        started = time.monotonic()
        async with UPSTREAM.open() as client:
            data, text, tool = await _call(client, "create_song", ours)
            songs, task = normalize.songs(data, text), normalize.task_id(data)
            if wait and WAIT > 0:
                songs = await _wait(client, songs, task, started + WAIT)
        status, name = _overall(songs), UPSTREAM.config()["name"]
        if status == "ready":
            message = ""
        elif status == "failed":
            message = f"{name} couldn't make this song: {normalize.brief(data, text, 300)}"
        elif songs:
            message = f"Still being made: check again with get_song, or find it in your {name} library."
        else:  # no songs in the answer: what the supplier said
            message = " ".join(text.split())[:500] or f"{name} started the song; it will appear in your library."
        return {"ok": True, "supplier": name, "tool": tool, "status": status, "songs": songs,
                "task_id": task if not songs else None, "message": message}
    return await _safely(work)


@mcp.tool(description="Songs' details and status (audio links when they're ready), by the ids create_song or list_songs "
                      "gave: one in song_id, or several (up to 12) in song_ids.")
async def get_song(song_id: str = "", song_ids: list[str] | None = None) -> dict:
    async def work():
        ids = list(dict.fromkeys(check_id(one) for one in [*([song_id] if song_id else []), *(song_ids or [])][:12]))
        if not ids:
            raise ValueError("Give a song id (song_id) or several (song_ids)")
        async with UPSTREAM.open() as client:
            if len(ids) == 1:
                data, text, tool = await _call(client, "get_song", {"song_id": ids[0]})
                songs = normalize.songs(data, text)
            else:
                songs, text = await _ask(client, [], ids, await _takes_many(client)), ""
                tool = mapping.plan("get_song", await UPSTREAM.list_tools(client), UPSTREAM.config()["ops"])["tool"]["name"]
        return {"ok": True, "supplier": UPSTREAM.config()["name"], "tool": tool, "status": _overall(songs), "songs": songs,
                "message": "" if songs else " ".join(text.split())[:500]}
    return await _safely(work)


@mcp.tool(description="Your latest songs in the music supplier's library (newest first), with audio links.")
async def list_songs(limit: int = 10) -> dict:
    async def work():
        count = max(1, min(int(limit), 50))
        async with UPSTREAM.open() as client:
            data, text, tool = await _call(client, "list_songs", {"limit": count})
        songs = normalize.songs(data, text)[:count]
        return {"ok": True, "supplier": UPSTREAM.config()["name"], "tool": tool, "count": len(songs), "songs": songs}
    return await _safely(work)


@mcp.tool(description="How many credits are left on the music supplier account (each song uses some).")
async def get_credits() -> dict:
    async def work():
        async with UPSTREAM.open() as client:
            data, text, tool = await _call(client, "get_credits", {})
        return {"ok": True, "supplier": UPSTREAM.config()["name"], "tool": tool,
                "credits": normalize.credits(data, text), "details": normalize.brief(data, text)}
    return await _safely(work)


@mcp.tool(description="The music supplier in use and the tools it offers, with which of them each of our tools uses. "
                      "For setting up a supplier (suppliers.json); not needed to make songs.")
async def supplier_tools() -> dict:
    async def work():
        entry = UPSTREAM.config()
        async with UPSTREAM.open() as client:
            tools = await UPSTREAM.list_tools(client)
        uses = {}
        for op in mapping.OPS:
            try:
                uses[op] = mapping.plan(op, tools, entry["ops"])["tool"]["name"]
            except LookupError:
                uses[op] = None
        return {"ok": True, "supplier": entry["name"], "key": entry["key"], "url": entry["url"], "auth": entry["auth"],
                "uses": uses, "tools": [{"name": t["name"], "description": " ".join(t["description"].split())[:300],
                                         "arguments": sorted((t["input_schema"].get("properties") or {}))} for t in tools]}
    return await _safely(work)


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
