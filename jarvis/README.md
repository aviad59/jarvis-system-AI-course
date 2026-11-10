# JARVIS console

A local web console that runs and monitors the RAG pipeline (exercises 1–3), built as the
starting point for a Jarvis-style assistant.

## Run

Step-by-step guide with screenshots: [RUNNING.md](RUNNING.md).

From the `session-4-students` folder, with the Ollama app running:

```
python -m pip install pymongo faster-whisper     # once
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu   # once, for Hebrew speech
python -m pip install transformers
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

## What you see

| Area | Shows |
|---|---|
| Systems | Ollama + models, MongoDB Atlas chunk count, Anthropic key, local index, Whisper, MMS-TTS |
| Protocols | Skills. *Ask the documents* = the RAG pipeline; *System check* = health report |
| Core | The current state: standing by, listening, processing (with stage), responding |
| Dialogue | Answers as claims with citation chips; click a chip to see the exact source chunk |
| Pipeline telemetry | Each stage with live timer and status, retrieved evidence with scores, raw log |
| Mission log | Every run, saved in `jarvis/data/runs/`; click to replay. Link a run with `/#run=<id>` |

Voice: click the mic (or Ctrl+Space) and speak. Recording stops when you pause and is
transcribed by OpenAI's Whisper model running locally (faster-whisper, model `small`;
change with `JARVIS_WHISPER_MODEL=medium`). The question then runs automatically, and the
answer is read aloud when *Voice replies* is on.

Speech out: Hebrew answers are spoken by Meta's MMS-TTS (`facebook/mms-tts-heb`, ~145 MB, downloaded
from Hugging Face on the first reply and cached), running locally on the CPU. Numbers are spelled out in
Hebrew first, since the model only reads letters. English uses the browser's voice (MMS `mms-tts-eng` when
the browser has none). If MMS-TTS is unavailable, Jarvis falls back to the browser voice. Override with
`JARVIS_TTS_MODEL_HE`, `JARVIS_TTS_MODEL_EN`, and speed with `JARVIS_TTS_RATE=1.1`. The MMS weights are
CC-BY-NC 4.0 (non-commercial).

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
* `tts.py` – local MMS-TTS text-to-speech (Hebrew, English).
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
