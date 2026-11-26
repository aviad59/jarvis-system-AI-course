# youtube-shorts MCP server

Free MCP server for public YouTube Shorts. It uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to read public pages,
so there is no API key, no account and no login.

| Tool | Returns |
|---|---|
| `get_youtube_shorts(channel, limit, details)` | A channel's latest Shorts (`@MrBeast` or a channel URL) |
| `search_youtube_shorts(query, limit, details)` | Shorts that match a keyword or hashtag |
| `get_post_details(url)` | Full details of one Short or video |

`details=true` adds likes, comment count, description and upload date (slower).

## Setup (once, from this folder)

```
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
```

## Used by
- **Claude Code**: `jarvis/.mcp.json` registers it as `youtube-shorts`. Approve it, then check with `/mcp`.
- **Jarvis**: the **YouTube Shorts** skill (`jarvis/jarvis/skills.py`) calls these tools through
  `jarvis/mcp_client.py`, Jarvis's MCP client. It reads the same `.mcp.json`, starts `server.py` with this folder's
  `.venv` (or Jarvis's own Python if there is none) on the first request, and keeps it running: later requests reuse
  the same process, so yt-dlp loads once. If the server crashes, the next request starts it again.
