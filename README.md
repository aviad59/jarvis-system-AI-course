# Jarvis system — AI course

A Jarvis-style assistant built session by session: a RAG pipeline over documents (session 4), then over
your own Obsidian vault with MCP tools for YouTube Shorts and memes (session 5).

| Folder | What's inside |
|---|---|
| [`session-4/`](session-4/) | The RAG exercises (indexing → retrieval → generation), `rag.py`, and the first Jarvis console. Start with `session-4/START-HERE.html`. |
| [`session-5/`](session-5/) | The current Jarvis. Start with `session-5/START-HERE.html` (Hebrew: `session-5/פתחו-כאן.html`). |
| [`session-5/jarvis-rag/`](session-5/jarvis-rag/) | Jarvis + the RAG pipeline over the Obsidian vault: live re-indexing, voice in/out (Hebrew + English), conversation memory, one "Ask Jarvis" box. |
| [`session-5/mcp-servers/social-mcp/`](session-5/mcp-servers/social-mcp/) | MCP server: public YouTube Shorts (free, no login). |
| [`session-5/mcp-servers/meme-mcp/`](session-5/mcp-servers/meme-mcp/) | MCP server: memes (open-source Memegen; Hebrew drawn locally). |
| [`session-5/workshop/`](session-5/workshop/) | The session 5 workshop exercises (vault, MCP, FastMCP). |

## Run the current Jarvis

With the Ollama app running (`ollama pull embeddinggemma` and `ollama pull gemma3:4b` once):

```
cd session-5/jarvis-rag
python -m pip install pymongo faster-whisper transformers edge-tts
python jarvis/server.py
```

Open http://127.0.0.1:8765. Details: [`session-5/jarvis-rag/README.md`](session-5/jarvis-rag/README.md).
The MCP servers need their own one-time setup; see the README in each folder.

## Secrets

API keys and database credentials stay in git-ignored `.env` files and are never committed:
`session-5/jarvis-rag/.env` (`ANTHROPIC_API_KEY=...` and/or `GEMINI_API_KEY=...`) and
`session-5/jarvis-rag/exercise-1-indexing/atlas-credentials.env` (MongoDB Atlas).
