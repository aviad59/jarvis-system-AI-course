# JARVIS console

A local web console that runs and monitors the RAG pipeline (exercises 1–3), built as the
starting point for a Jarvis-style assistant.

## Run

Step-by-step guide with screenshots: [RUNNING.md](RUNNING.md).

From the `jarvis` folder (at the root of the repository), with the Ollama app running:

```
python -m pip install pymongo faster-whisper     # once
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu   # once, for Hebrew speech
python -m pip install transformers edge-tts
python jarvis/server.py
```

Install into the same Python that runs the server. If your terminal activates
`exercise-1-indexing\.venv` (VS Code does this automatically), `pip` installs there.
If voice fails, the error message names the exact interpreter that is missing the module.

Open http://127.0.0.1:8765. The server listens on this computer only. Stop it with Ctrl+C.

The chunk vectors are read from `exercise-1-indexing/build/index.json` by default (no database
needed). Choose **Vectors from: mongodb** to use MongoDB Atlas instead.

Answers are generated locally by Ollama (`gemma3:4b`) by default. To answer with Claude instead,
put `ANTHROPIC_API_KEY=...` in a `.env` file next to `rag.py` and choose **Answer with: anthropic**.
To answer with Google Gemini (`gemini-3.8-flash`), put `GEMINI_API_KEY=...` there and choose
**Answer with: gemini**. `GOOGLE_API_KEY` is accepted too.

## Obsidian vault (session 5)

**Documents** chooses what is indexed: `vault` (default), `course` (the incident documents in
`exercise-1-indexing/documents/`), or `both`. The vault is `~/Documents/Obsidian Vault`; set
`JARVIS_VAULT=<folder>` to use another one. Every note is indexed, in any folder; `.obsidian`, `Templates`,
`Drafts` and `indexing-scope.md` never are. `JARVIS_VAULT_INCLUDE=Projects,Meetings` limits it to those folders.
Each chunk keeps the note's path, `meeting_date` and `project_id` (from Obsidian Properties).
**Live updates:** while the server runs, `vault_watch.py` checks the vault every 2 s. When a note is
added, edited or deleted, it waits until the vault has been quiet for 2 s, then starts a **Sync vault**
run (visible in Mission log). Only new or changed chunks are embedded. A question asked during a sync
waits for it to finish. As a safety net, every question also checks the notes against the index and
re-indexes first if they differ, even with **Skip indexing** on. Switching **Documents** rebuilds too.
The Systems panel shows the watcher's state.

## What you see

| Area | Shows |
|---|---|
| Systems | Ollama + models, MongoDB Atlas chunk count, Anthropic key, Obsidian vault, local index, Whisper, speech engine |
| Protocols | Skills. *Ask Jarvis* (default) = one box for everything, see below; *Ask the documents* = the RAG pipeline; *YouTube Shorts* = the MCP tool; *System check* = health report |
| Core | The current state: standing by, listening, processing (with stage), responding |
| Dialogue | Answers as claims with citation chips; click a chip to see the exact source chunk |
| Pipeline telemetry | Each stage with live timer and status, retrieved evidence with scores, raw log |
| Mission log | Every run, saved in `jarvis/data/runs/`; click to replay. Link a run with `/#run=<id>` |
| Feedback | Under every finished answer: **▲ Good** or **⚑ Report a problem**: tick boxes (slow response, wrong behavior, misunderstood the request, incorrect / made-up answer, incomplete, bad sources, wrong language, voice problem, wrong video or meme, other) plus an optional comment. Saved on the run (shown in the Mission log) and appended to `jarvis/data/runs/feedback.jsonl` with the question, skill, model and duration; `GET /api/feedback` lists the latest reports |

### Ask Jarvis: one box for everything

With **Ask Jarvis** selected (the default), type or say anything; `router.py` decides where it goes
and the card shows the choice (`JARVIS · YOUTUBE SHORTS`, and the reason in the log):

| You say | Goes to |
|---|---|
| a question — `מה הוחלט בישיבת הצוות?`, `What was the error rate in September?` | your documents (RAG, with citations) |
| `find me 5 youtube shorts about cooking`, `תמצא לי שורטס על בישול` | YouTube Shorts MCP · search (`5` sets the number of results) |
| `@MrBeast`, `show me shorts from channel MrBeast`, `סרטונים מהערוץ MrBeast` | YouTube Shorts MCP · latest Shorts of the channel |
| a YouTube link | YouTube Shorts MCP · details of that video |
| `tell me a joke about programmers`, `תספר לי בדיחה`, `תצחיק אותי` | Memes MCP · a joke is answered with a meme (a question *about* a joke in the notes stays with the documents) |
| `make a meme about monday meetings`, `תעשה לי מם על ישיבות`, `drake meme: writing tests / shipping on friday` | your notes are searched first (retrieval only), then the memes MCP makes the meme from what they say; the card names the notes used. Give your own captions to skip both; *Meme: use my notes* off = topic only |
| `מה הצוות אוהב לאכול? תמצא לי שורטס על זה`, `find shorts about what the team likes to eat` | **notes → Shorts**: answer from the notes, turn the answer into a search (`פיצה פיתה`), then the MCP tool |
| `system check` / `בדיקת מערכות`, `sync the vault` / `סנכרן את הכספת` | system check / vault re-index |
| `/docs …`, `/shorts …`, `/meme …` | that skill, whatever the words |

Anything not clearly about YouTube or the system goes to the documents, so a question about the
notes that mentions YouTube (`who owns the youtube channel project?`) is answered from the notes.
Add `/shorts` when in doubt.

**Who decides: the rules or the model** (option *Route with*; default from `JARVIS_ROUTER=rules|model`):
- `rules` (default): `router.route()` only, plain patterns: instant and offline, but every new skill needs new patterns.
- `model`: `/commands`, YouTube links, a bare `@handle`, `system check` and `sync the vault` are sure, so they
  go straight to their skill. For everything else the answer model (*Answer with*: Ollama `gemma3:4b` or Claude)
  gets the list of tools (`router.TOOLS`) and picks one tool and its input. This happens in the *Understand request*
  stage, so the rest of the pipeline appears once the choice is made. The descriptions of the MCP tools come
  live from each server's `list_tools()` (read once when Jarvis starts). If the model fails (Ollama off, a bad
  reply), the rules decide and the stage turns yellow.
  Measured with `gemma3:4b` on a CPU (no GPU): ~17 s per request and 29 of 41 test phrases right (the rules: 40);
  it mostly sends plain questions that mention YouTube, memes or the team to notes → Shorts. Worth it with a GPU or Claude.

The router calls Ollama without a JSON `format`. Any `format` turns off Ollama's prompt cache, so the
fixed tool list (~500 tokens) would be read again on every call; the reply is checked in `router.from_tool` instead.

**Conversation:** the console sends the last 6 questions and answers of this page with each request, so
follow-ups work. A follow-up such as `והראל?` or `what about Harel?` is first rewritten into a full question
(*Understand follow-up* stage: `מה הראל אוהב לאכול?`) so retrieval finds the right note, and the answer model
also sees the earlier turns (as context only: facts still come from the cited notes). `find shorts about it` /
`תמצא לי שורטס על זה` right after an answer searches YouTube from that answer, without a new notes search.
Reloading the page starts a new conversation.

**Notes → Shorts** (RAG + MCP): a YouTube request that points at the notes ("about it", "על זה", or names the
team, a meeting, a project) runs both. First the notes answer (with citations, as usual). Then the same model
(Ollama or Claude) turns *the answer* into a short search query, and the youtube-shorts MCP tool runs it. If the
query model fails, the answer's own words are searched; if the notes have no answer, nothing is searched.
Jarvis reads the answer aloud, says what it found, then plays the first Short.
A Shorts request whose topic appears in your notes (`תמצא לי שורטס על עידן`, where Idan is in a note) also
goes to the notes first ("what do the notes say about Idan?") and searches YouTube with that answer; if the
notes say nothing, the topic is searched as asked. `/shorts עידן` skips the notes.
Names match across scripts by sound: `עומר ברנשטיין` finds **Omer Bernstein** in a WhatsApp export (`brnstn` both
ways); a short first name alone (`עומר`) counts when the notes use it often. Memes and jokes work the same way:
`תיצור לי בדיחה על עומר ברנשטיין` asks the notes about Omer, then writes the meme from that answer (`/meme …`
skips the notes). A request that only points back (`סרטון שמייצג את היחסים ביניהם`, `this situation`) uses the
previous answer; `תחפש מידע על X ואז תביא סרטון על המידע הזה` runs the notes, then YouTube.

Voice: click the mic (or Ctrl+Space) and speak. The question runs automatically when you
pause, and the answer is read aloud the moment it arrives when *Voice replies* is on (no Speak click). If the recognizer isn't
confident about what it heard, the text is left in the box for you to check and send with
Enter instead of running the wrong question. *Recognition* picks the engine:

| Engine | Speed | Privacy | Where |
|---|---|---|---|
| **Browser (fast)**, default | instant, text appears while you speak | audio goes to the browser's speech service (Google for Chrome, Microsoft for Edge) | Chrome, Edge |
| **Whisper (local)** | ~3 s per question on a CPU (`small`) | nothing leaves this computer | every browser |

If the browser engine fails (offline, Brave, Firefox), Jarvis switches to local Whisper by itself.
Whisper recording stops when you pause (the threshold adapts to the room's background noise;
if you say nothing for 8 s it gives up).

How `voice.py` improves on a plain Whisper call: quiet recordings are level-normalized;
titles and identifiers from the RAG index (service names, IDs such as `SYS-001`) are passed
to Whisper as hotwords, so they come back spelled the way the notes spell them; *Auto*
language chooses only between Hebrew and English; Whisper's known hallucinations on silence
("Thanks for watching", subtitle credits) are dropped; and it uses a CUDA GPU automatically
when one is available.

| Variable | Default | Effect |
|---|---|---|
| `JARVIS_WHISPER_MODEL` | `small` | model for every language; `large-v3-turbo` is more accurate, ~10 s per question on a CPU |
| `JARVIS_WHISPER_MODEL_HE` | — | Hebrew-only model: `ivrit-ai/whisper-large-v3-turbo-ct2` (Whisper fine-tuned on Hebrew by ivrit.ai, ~1.6 GB) is the best local Hebrew, but ~10 s per question on a CPU; use it with an NVIDIA GPU |
| `JARVIS_WHISPER_DEVICE` | `auto` | `cpu`, `cuda`, or `auto` (GPU when usable) |
| `JARVIS_WHISPER_LANGUAGES` | `he,en` | languages *Auto* may pick |
| `JARVIS_WHISPER_HOTWORDS` | — | extra comma-separated vocabulary, e.g. `Cedar, Atlas, Ollama` |
| `JARVIS_WHISPER_THREADS` | all cores | CPU threads for Whisper |

Speech out: answers are spoken by Microsoft's neural voices through `edge-tts`: `he-IL-AvriNeural` for Hebrew
(including the English terms inside it) and `en-US-GuyNeural` for English. The audio is streamed to the
browser as it is synthesized, so playback starts in about 1–2 s; a replayed answer is instant (cached).
**This needs internet, and the spoken text is sent to Microsoft's speech service.** Set
`JARVIS_TTS_ENGINE=local` to keep speech fully on this computer.

YouTube Shorts: results play inside the console (an embedded YouTube player, no links out). The answer card
shows thumbnails; the Short plays large over the center column, with the other results beside it (✕ or Esc
closes it, a thumbnail reopens it). With *Voice replies* on, Jarvis first says what it found, then opens the
first Short.

While Jarvis speaks, the core (the circle at the top) talks: the nucleus and a ring of voice bars move with
the actual sound of the voice.

Offline fallback: if the service can't be reached, Jarvis switches automatically to Meta's MMS-TTS running
locally on the CPU (`facebook/mms-tts-heb` / `mms-tts-eng`, preloaded in the background at start), and tries
the neural voice again after a minute. Numbers are spelled out in Hebrew for MMS, since it only reads letters.

| Variable | Default | Effect |
|---|---|---|
| `JARVIS_TTS_ENGINE` | `edge` | `local` = MMS only, nothing leaves the computer |
| `JARVIS_TTS_VOICE_HE` / `JARVIS_TTS_VOICE_EN` | `he-IL-AvriNeural` / `en-US-GuyNeural` | e.g. `he-IL-HilaNeural`, `en-GB-RyanNeural` |
| `JARVIS_TTS_RATE` | `1.0` | speaking speed for both engines (`1.1` = 10% faster) |
| `JARVIS_TTS_MODEL_HE` / `JARVIS_TTS_MODEL_EN` | MMS models | local fallback models (CC-BY-NC 4.0 weights) |

## Architecture

```
browser (static/)  --POST /api/runs-->  server.py --> RunManager (runs.py) --> Skill.run() (skills.py)
       ^                                                      |                    |
       +------- Server-Sent Events: /api/runs/<id>/events ----+     child processes: the exercise scripts
                                                                     MCP servers (.mcp.json): mcp_client.py
```

* `skills.py` – what Jarvis can do. One class per skill; `JarvisSkill` runs whichever skill the router picks.
* `router.py` – which skill a free-form request belongs to: the tool list and prompt for the model
  (`TOOLS`, `tool_prompt`, `from_tool`), the sure cases (`sure_route`) and the plain-pattern rules (`route`).
* `runs.py` – a run is an ordered list of events; live viewers follow it, finished runs are saved.
* `status.py` – health checks (never returns URIs or keys).
* `mcp_client.py` – Jarvis's MCP client (standard library only): one long-lived stdio connection per server in
  `jarvis/.mcp.json`, shared by the skills. A server starts on first use, is restarted if it died, and stops with
  Jarvis. `call_tool(server, tool, arguments)`, `list_tools(server)`, `servers()`.
* `voice.py` – local Whisper speech-to-text.
* `tts.py` – text-to-speech: streamed neural voices (edge-tts), local MMS-TTS fallback (Hebrew, English).
* `server.py` – HTTP API, SSE stream, static files (standard library only).

## Adding a skill

```python
class WeatherSkill(Skill):
    name, title, description = "weather", "Weather", "Today's forecast."
    needs_question = False
    options = [{"key": "city", "label": "City", "type": "text", "default": "Tel Aviv"}]

    def stages(self, request):
        return [{"id": "fetch", "title": "Fetch forecast"}]

    def run(self, request, run):
        run.emit("stage_start", stage="fetch", title="Fetch forecast")
        ...                                   # do the work; run.run_process(...) streams a command's output
        run.emit("stage_end", stage="fetch", status="ok", seconds=0.4)
        run.emit("message", text="Sunny, 24°C.")
```

Add an instance to `SKILLS` in `skills.py`. The console builds its button and option controls
automatically. A new event type only needs a handler in `HANDLERS` in `static/app.js`.

## Tests

```
python jarvis/check_jarvis.py
```

The server runs inside the test process on a random port, with a fake skill, so the tests need
no model, database or key.
