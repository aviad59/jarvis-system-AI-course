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

## Chat exports: filter by sender and date (MongoDB)

A WhatsApp export in the vault (`# WhatsApp Chat Export: ...`, e.g. `chat.md`) is chunked by whole messages
(`exercise-1-indexing/chat_parser.py`). Each chunk keeps `embed_text` (contents only, what is embedded),
`display_text` (`Sender [YYYY-MM-DD HH:MM]: text`, what the answer model reads), `messages`, `participants`
and `start_ts`/`end_ts` (UTC; the export's times are `CHAT_TIMEZONE`, default `Asia/Jerusalem`).

```
python -m pip install tzdata                      # Windows: time zone data for zoneinfo
cd exercise-1-indexing
python ingest_chats.py --dry-run                  # parsed messages + chunk stats, writes nothing
python ingest_chats.py                            # embed + upsert into Atlas (idempotent), indexes, participants
```

With **Vectors from: mongodb**, *Ask the documents* first extracts sender/time filters from the question
(*Chat filters* stage, the answer model; turn off with *Chat filters (self-query)*). The filters run inside the
same `$vectorSearch`; if nothing matches they are relaxed (time first, then senders) and the result says so.
By hand: `python rag.py --source mongodb --question "..." --senders "Omer Bernstein" --from 2026-08-01 --to 2026-08-31`
or `--self-query`. With `local`, chats are indexed the same way but cannot be filtered.
After the migration the vault watcher keeps the chat in MongoDB up to date (when the last question used mongodb).

## Tests

```
python jarvis/check_jarvis.py
python exercise-1-indexing/check_chat_parser.py   # run from inside the folder
python exercise-2-retrieval/check_chat_filters.py # run from inside the folder
python chat_examples.py                           # end to end, after ingest_chats.py (Ollama + Atlas)
```

Details: `jarvis/README.md`, step-by-step with screenshots: `jarvis/RUNNING.md`.
