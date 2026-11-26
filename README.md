# Jarvis system — AI course

A Jarvis-style assistant built session by session: a RAG pipeline over documents (session 4), then over
your own Obsidian vault with MCP tools for YouTube Shorts and memes (session 5). Jarvis itself lives in
its own folder and keeps growing; the session folders hold each session's material.

| Folder | What's inside |
|---|---|
| [`jarvis/`](jarvis/) | **The current Jarvis.** The console, voice in/out (Hebrew + English), conversation memory, one "Ask Jarvis" box, and the RAG pipeline over the Obsidian vault with live re-indexing. |
| [`jarvis/mcp-servers/social-mcp/`](jarvis/mcp-servers/social-mcp/) | MCP server: public YouTube Shorts (free, no login). |
| [`jarvis/mcp-servers/meme-mcp/`](jarvis/mcp-servers/meme-mcp/) | MCP server: memes (open-source Memegen; Hebrew drawn locally). |
| [`session-4/`](session-4/) | The RAG exercises (indexing → retrieval → generation), `rag.py`, and the first Jarvis console, as they were in session 4. Start with `session-4/START-HERE.html`. |
| [`session-5/`](session-5/) | Session 5 material: start with `session-5/START-HERE.html` (Hebrew: `session-5/פתחו-כאן.html`), and the workshop exercises (vault, MCP, FastMCP) in `session-5/workshop/`. |

## Run Jarvis

With the Ollama app running (`ollama pull embeddinggemma` and `ollama pull gemma3:4b` once):

```
cd jarvis
python -m pip install pymongo faster-whisper transformers edge-tts
python jarvis/server.py
```

Open http://127.0.0.1:8765. Details: [`jarvis/README.md`](jarvis/README.md).
The MCP servers need their own one-time setup; see the README in each folder.

## Secrets

API keys and database credentials stay in git-ignored `.env` files and are never committed:
`jarvis/.env` (`ANTHROPIC_API_KEY=...`) and
`jarvis/exercise-1-indexing/atlas-credentials.env` (MongoDB Atlas).
