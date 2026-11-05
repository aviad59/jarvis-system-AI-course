# JARVIS console

A local web console that runs and monitors the RAG pipeline (exercises 1–3), built as the
starting point for a Jarvis-style assistant.

## Run

From the `session-4-students` folder, with the Ollama app running:

```
python -m pip install pymongo faster-whisper     # once
python jarvis/server.py
```

Install into the same Python that runs the server. If your terminal activates
`exercise-1-indexing\.venv` (VS Code does this automatically), `pip` installs there.
If voice fails, the error message names the exact interpreter that is missing the module.

Open http://127.0.0.1:8765. The server listens on this computer only. Stop it with Ctrl+C.

To answer with Claude, put `ANTHROPIC_API_KEY=...` in a `.env` file next to `rag.py`, or
choose **Answer with: ollama** to stay fully local.

## What you see

| Area | Shows |
|---|---|
| Systems | Ollama + models, MongoDB Atlas chunk count, Anthropic key, local index, Whisper |
| Protocols | Skills. *Ask the documents* = the RAG pipeline; *System check* = health report |
| Core | The current state: standing by, listening, processing (with stage), responding |
| Dialogue | Answers as claims with citation chips; click a chip to see the exact source chunk |
| Pipeline telemetry | Each stage with live timer and status, retrieved evidence with scores, raw log |
| Mission log | Every run, saved in `jarvis/data/runs/`; click to replay. Link a run with `/#run=<id>` |

Voice: click the mic (or Ctrl+Space) and speak. Recording stops when you pause and is
transcribed by OpenAI's Whisper model running locally (faster-whisper, model `small`;
change with `JARVIS_WHISPER_MODEL=medium`). The question then runs automatically, and the
answer is read aloud when *Voice replies* is on.

## Architecture

```
browser (static/)  --POST /api/runs-->  server.py --> RunManager (runs.py) --> Skill.run() (skills.py)
       ^                                                      |                    |
       +------- Server-Sent Events: /api/runs/<id>/events ----+     child processes: the exercise scripts
```

* `skills.py` – what Jarvis can do. One class per skill.
* `runs.py` – a run is an ordered list of events; live viewers follow it, finished runs are saved.
* `status.py` – health checks (never returns URIs or keys).
* `voice.py` – local Whisper speech-to-text.
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
