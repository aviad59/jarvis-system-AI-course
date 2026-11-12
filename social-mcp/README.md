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
- **Claude Code**: `.mcp.json` in the repo root registers it as `youtube-shorts`. Approve it, then check with `/mcp`.
- **Jarvis**: the **YouTube Shorts** skill (`Session5/jarvis-rag/jarvis/skills.py`) runs `jarvis_client.py`,
  which connects to `server.py` over MCP stdio and calls one tool.
