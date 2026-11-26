# Free MCP server for public YouTube Shorts.
# Uses yt-dlp to read public pages: no API key, no account, no login, no cost.
import functools
import sys
import time

from fastmcp import FastMCP
from yt_dlp import YoutubeDL

mcp = FastMCP("youtube-shorts")
NAME = "youtube-shorts"

def log(message):  # stderr is the server's log (stdout is the MCP channel): Jarvis shows it in the Mission log
    print(f"{NAME}: {message}", file=sys.stderr, flush=True)


def logged(tool):  # each call's start, outcome and time in the log (the arguments are in Jarvis's "MCP: call" line)
    @functools.wraps(tool)
    def run(*args, **kwargs):
        started = time.monotonic()
        log(f"{tool.__name__} ...")
        try:
            result = tool(*args, **kwargs)
        except Exception as error:
            log(f"{tool.__name__} failed in {time.monotonic() - started:.1f}s: {type(error).__name__}: "
                f"{' '.join(str(error).split())[:200]}")
            raise
        seconds = time.monotonic() - started
        if isinstance(result, dict) and not result.get("ok", True):
            log(f"{tool.__name__} failed in {seconds:.1f}s: {result.get('error')}")
        else:
            count = result.get("count") if isinstance(result, dict) else None
            log(f"{tool.__name__} done in {seconds:.1f}s{f': {count} result(s)' if count is not None else ''}")
        return result
    return run

MAX_LIMIT = 25


def _ydl(flat: bool) -> YoutubeDL:
    return YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist" if flat else False,
        "ignoreerrors": True,
    })


def _post(info: dict) -> dict:
    """Keep only the fields that are useful to the model."""
    return {
        "id": info.get("id"),
        "url": info.get("webpage_url") or info.get("url"),
        "title": info.get("title"),
        "description": info.get("description"),
        "uploader": info.get("uploader") or info.get("channel"),
        "upload_date": info.get("upload_date"),  # YYYYMMDD
        "duration_sec": info.get("duration"),
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "comments": info.get("comment_count"),
        "tags": info.get("tags"),
        "thumbnail": info.get("thumbnail"),
    }


def _list(url: str, limit: int, details: bool) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    with _ydl(flat=True) as ydl:
        playlist = ydl.extract_info(url, download=False)
    if not playlist:
        return {"ok": False, "error": f"Could not read {url}", "posts": []}
    entries = [e for e in (playlist.get("entries") or []) if e][:limit]
    if details:
        full = []
        with _ydl(flat=False) as ydl:
            for e in entries:
                link = e.get("url") or e.get("webpage_url")
                info = ydl.extract_info(link, download=False) if link else None
                full.append(info or e)
        entries = full
    return {"ok": True, "source": url, "count": len(entries), "posts": [_post(e) for e in entries]}


@mcp.tool(description="Get the latest YouTube Shorts of a channel. `channel` is a handle like '@MrBeast' or a channel URL. "
                      "Set details=true for likes, comments, description and date (slower).")
@logged
def get_youtube_shorts(channel: str, limit: int = 10, details: bool = False) -> dict:
    channel = channel.strip().rstrip("/")
    if not channel.startswith("http"):
        channel = "https://www.youtube.com/" + (channel if channel.startswith("@") else "@" + channel)
    if not channel.endswith("/shorts"):
        channel += "/shorts"
    return _list(channel, limit, details)


@mcp.tool(description="Search YouTube Shorts by keyword or hashtag. Set details=true for likes, comments, description and date (slower).")
@logged
def search_youtube_shorts(query: str, limit: int = 10, details: bool = False) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    q = query.strip()
    if "#shorts" not in q.lower():
        q += " #shorts"
    return _list(f"ytsearch{limit}:{q}", limit, details)


@mcp.tool(description="Get full details of a single public YouTube Short or video URL.")
@logged
def get_post_details(url: str) -> dict:
    with _ydl(flat=False) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return {"ok": False, "error": f"Could not read {url}"}
    return {"ok": True, "post": _post(info)}


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
