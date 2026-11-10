"""Jarvis console server: runs skills and streams their progress to the browser.

Standard library only (plus the pipeline's own dependencies). Binds to 127.0.0.1 by default,
so it is reachable only from this computer.

  python jarvis/server.py            -> http://127.0.0.1:8765

API
  GET  /api/status[?force=1]          system health (cached 20 s)
  GET  /api/skills                    skills and their options
  GET  /api/runs                      {active, runs: [history rows]}
  POST /api/runs                      {skill, request} -> 202 {run_id} | 409 busy | 400 invalid
  GET  /api/runs/<id>                 full run: summary, request, events
  GET  /api/runs/<id>/events?from=N   Server-Sent Events, live; resumes from N or Last-Event-ID
  POST /api/runs/<id>/cancel          stop the running stage
  POST /api/transcribe?language=he    raw audio body -> {text, ...} via local Whisper
  POST /api/speak                     {text, language?} -> audio/wav via local MMS-TTS
"""
import argparse  # host / port
import json  # API bodies
import mimetypes  # static file types
import sys  # import paths
import threading  # background model warm-up
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # one thread per connection (SSE stays open)
from pathlib import Path  # static files
from urllib.parse import parse_qs, urlparse  # query strings

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # rag.py
sys.path.insert(0, str(HERE))  # sibling modules
import skills  # noqa: E402
import tts  # noqa: E402
import status  # noqa: E402
import voice  # noqa: E402
from runs import Busy, RunManager  # noqa: E402

STATIC = HERE / "static"
MAX_JSON_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    manager = None  # set in make_server
    server_version = "Jarvis/0.1"

    # -- helpers -------------------------------------------------------------
    def send_json(self, value, code=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_audio(self, body, info):
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-TTS-Info", json.dumps(info))  # ASCII only: model, language, timings
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, message):
        self.send_json({"error": message}, code)

    def read_body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("Request body too large")
        return self.rfile.read(length) if length else b""

    def log_message(self, format, *args):  # quiet: only errors, not every poll
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            super().log_message(format, *args)

    # -- routing -------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                return self.static("index.html")
            if parts[:1] == ["static"] and len(parts) == 2:
                return self.static(parts[1])
            if url.path == "/api/status":
                return self.send_json(status.check_all(force=query.get("force") == ["1"]))
            if url.path == "/api/skills":
                return self.send_json(skills.describe())
            if url.path == "/api/runs":
                return self.send_json({"active": self.manager.active_id(), "runs": self.manager.history()})
            if parts[:2] == ["api", "runs"] and len(parts) in (3, 4):
                run = self.manager.get(parts[2])
                if not run:
                    return self.error(404, "Run not found")
                if len(parts) == 3:
                    return self.send_json(run.to_json())
                if parts[3] == "events":
                    if query.get("from"):  # explicit position
                        start = int(query["from"][0])
                    elif self.headers.get("Last-Event-ID"):  # browser auto-reconnect: continue after the last event it saw
                        start = int(self.headers["Last-Event-ID"]) + 1
                    else:
                        start = 0
                    return self.stream(run, max(0, start))
            self.error(404, "Not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the browser went away

    def do_POST(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            # Always consume the body before replying: closing a socket with unread data makes Windows reset the connection.
            raw = self.read_body(voice.MAX_AUDIO_BYTES if url.path == "/api/transcribe" else MAX_JSON_BODY)
            if url.path == "/api/runs":
                body = json.loads(raw or b"{}")
                run = self.manager.start(body.get("skill", "rag"), body.get("request", {}))
                return self.send_json({"run_id": run.id}, 202)
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "cancel":
                run = self.manager.get(parts[2])
                if not run:
                    return self.error(404, "Run not found")
                run.cancel()
                return self.send_json({"ok": True})
            if url.path == "/api/transcribe":
                language = parse_qs(url.query).get("language", [""])[0][:5] or None
                audio = raw
                kind = (self.headers.get("Content-Type") or "").split(";")[0]
                suffix = {"audio/ogg": ".ogg", "audio/wav": ".wav", "audio/mp4": ".m4a", "audio/mpeg": ".mp3"}.get(kind, ".webm")
                return self.send_json(voice.transcribe(audio, language, suffix))
            if url.path == "/api/speak":
                body = json.loads(raw or b"{}")
                language = str(body.get("language") or "")[:5] or None
                return self.send_audio(*tts.synthesize(body.get("text", ""), language))
            self.error(404, "Not found")
        except Busy as error:
            self.error(409, str(error))
        except ImportError as error:  # name the module and the interpreter: a different venv is the usual cause
            package = {"faster_whisper": "faster-whisper", "av": "faster-whisper"}.get(error.name, error.name)
            self.error(503, f"Missing module '{error.name}' in the Python running the server ({sys.executable}). "
                            f'Install it there: "{sys.executable}" -m pip install {package}')
        except (ValueError, TypeError) as error:
            self.error(400, str(error))
        except Exception as error:  # unexpected: report the type, keep the server alive
            self.error(500, f"{type(error).__name__}: {error}")

    # -- responses -----------------------------------------------------------
    def static(self, name):
        path = (STATIC / name).resolve()
        if path.parent != STATIC.resolve() or not path.is_file():  # no path traversal
            return self.error(404, "Not found")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(name)[0] or "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def stream(self, run, seq):  # Server-Sent Events: one 'data:' line per event, until the run ends
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        while True:
            events, done = run.events_since(seq)
            for event in events:
                self.wfile.write(f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                seq = event["seq"] + 1
            if not events:
                self.wfile.write(b": keep-alive\n\n")  # stops proxies and browsers from timing out
            self.wfile.flush()
            if done and seq >= len(run.events):
                return


def make_server(host, port, data_dir=None):
    Handler.manager = RunManager(skills.SKILLS, data_dir) if data_dir else RunManager(skills.SKILLS)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True  # Ctrl+C doesn't wait for open event streams
    return server


def warm_up():  # load Whisper and the TTS voices once, in the background; failures only mean a slower first use
    for name, task in (("Whisper", lambda: voice.model() if voice.available() else None), ("MMS-TTS", tts.warm_up)):
        try:
            task()
            print(f"WARM-UP: {name} ready", flush=True)
        except Exception as error:
            print(f"WARM-UP: {name} skipped ({type(error).__name__}: {error})", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Jarvis console for the RAG pipeline")
    parser.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = this computer only")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    threading.Thread(target=warm_up, daemon=True).start()  # models load while you open the page
    print(f"JARVIS console: http://{args.host}:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
