# Jarvis + Obsidian (RAG pipeline)

Jarvis answers questions from the notes in your Obsidian vault, with citations back to each note.
Everything needed to run it is in this folder.

## What's here, in pipeline order

| Folder / file | Step | What it does |
|---|---|---|
| `exercise-1-indexing/` | 1. Index | Reads the vault notes (and/or `documents/`), splits them into chunks and embeds them with Ollama → `build/index.json` |
| `exercise-2-retrieval/` | 2. Retrieve | Finds the chunks closest to the question (cosine, optionally hybrid BM25) |
| `exercise-3-generation/` | 3. Answer | Ollama (`gemma3:4b`) or Claude answers using only those chunks, with citations |
| `rag.py` | 1 → 2 → 3 | Runs the three steps in order (command line) |
| `jarvis/` | Console | Web console + voice + **vault watcher** that re-indexes when notes change |
| `mcp-servers/social-mcp/` | MCP tool | **YouTube Shorts** skill: Jarvis calls the free `youtube-shorts` MCP server (no login). *Ask Jarvis* sends YouTube requests there automatically. Set it up once, see `social-mcp/README.md` |
| `mcp-servers/meme-mcp/` | MCP tool | **Memes** skill: Jarvis calls the `memes` MCP server (open-source Memegen, free; Hebrew drawn locally). *Ask Jarvis* sends "make a meme about ..." there. See `meme-mcp/README.md` |
| `mcp-servers/music-mcp/` | MCP tool | **Songs** skill: Jarvis calls the `music` MCP server, which talks to Suno's official MCP server (or another supplier in `suppliers.json`). *Ask Jarvis* sends "make a song about ..." there. Sign in once with `login.py`. See `music-mcp/README.md` |

Vault: `C:\Users\maest\Documents\Obsidian Vault` by default (set `JARVIS_VAULT` to change it).
Every note in the vault is indexed, in any folder (a new folder too); `.obsidian`, `Templates`, `Drafts`
and `indexing-scope.md` are always skipped. To index only some folders: `JARVIS_VAULT_INCLUDE=Projects,Meetings`.

## 1. Setup (once)

```
ollama pull embeddinggemma
ollama pull gemma3:4b
python -m pip install pymongo faster-whisper
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
python -m pip install transformers edge-tts
```

Optional secrets (git-ignored, not copied here). Put them back by hand if you use them:
- `.env` next to `rag.py` with `ANTHROPIC_API_KEY=...` → **Answer with: anthropic**
- `.env` next to `rag.py` with `GEMINI_API_KEY=...` ([Google AI Studio](https://aistudio.google.com/apikey)) → **Answer with: gemini**
- `exercise-1-indexing/atlas-credentials.env` → **Vectors from: mongodb**

## 2. Run Jarvis (Terminal 1, Ollama app open)

```
cd jarvis
python jarvis/server.py
```

Open http://127.0.0.1:8765. Expect `VAULT WATCH: watching ...Obsidian Vault`, then a first
re-index of the vault. After that, every new, edited or deleted note is re-indexed within a few seconds.

## Or: one question from the command line

```
python rag.py --question "מה סוכם בישיבת הצוות?" --documents vault
```

## Tests

```
python jarvis/check_jarvis.py
```

Details: `jarvis/README.md`, step-by-step with screenshots: `jarvis/RUNNING.md`.
