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
| `../../social-mcp/` | MCP tool | **YouTube Shorts** skill: Jarvis calls the free `youtube-shorts` MCP server (no login). Set it up once, see `social-mcp/README.md` |

Vault: `C:\Users\maest\Documents\Obsidian Vault` by default (set `JARVIS_VAULT` to change it).
Only `Projects/` and `Meetings/` are indexed (the whole vault if neither exists); `.obsidian`,
`Templates`, `Drafts` and `indexing-scope.md` are always skipped.

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
- `exercise-1-indexing/atlas-credentials.env` → **Vectors from: mongodb**

## 2. Run Jarvis (Terminal 1, Ollama app open)

```
cd "Session5\jarvis-rag"
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
