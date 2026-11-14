"""Runs: one execution of a skill, recorded as an ordered list of events.

Every event is a dict {'seq', 'type', 'time', ...}. The browser follows a run live over
Server-Sent Events and can reconnect from any seq; finished runs are saved to data/runs/
so the history survives a server restart.
"""
import json  # persist runs
import os  # child process environment
import subprocess  # run pipeline stages
import threading  # background execution and live notification
import time  # timestamps and durations
import uuid  # unique run IDs
from pathlib import Path  # files

DATA = Path(__file__).resolve().parent / "data" / "runs"
MAX_LOG_LINES = 5000  # a runaway process can't fill memory


class Cancelled(Exception):  # raised inside a skill when the user presses Cancel
    pass


class Busy(Exception):  # only one run at a time: pipeline stages share output files
    pass


class Run:
    def __init__(self, skill, request):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.skill, self.request = skill, request
        self.events, self.status = [], "running"
        self.started, self.finished = time.time(), None
        self.cond = threading.Condition()  # wakes up every live viewer when an event is added
        self.process, self.cancelled, self.log_lines = None, False, 0

    # -- events --------------------------------------------------------------
    def emit(self, type, **data):  # append an event and notify viewers
        with self.cond:
            self.events.append({"seq": len(self.events), "type": type, "time": round(time.time(), 3), **data})
            self.cond.notify_all()

    def events_since(self, seq, timeout=15):  # (new events, run finished?) - blocks until something happens
        with self.cond:
            if len(self.events) <= seq and self.status == "running":
                self.cond.wait(timeout)
            return self.events[seq:], self.status != "running"

    # -- processes -----------------------------------------------------------
    def run_process(self, stage, args, cwd, env):  # stream a child process's output as log events; return its exit code
        self.check_cancelled()
        child_env = {**os.environ, **env, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}  # unbuffered = live logs
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(args, cwd=cwd, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace", creationflags=flags)
        try:
            for line in self.process.stdout:  # ends when the process exits or is terminated by cancel()
                if self.log_lines < MAX_LOG_LINES:
                    self.log_lines += 1
                    self.emit("log", stage=stage, line=line.rstrip("\n"))
            return self.process.wait()
        finally:
            self.process = None

    def cancel(self):
        self.cancelled = True
        process = self.process
        if process and process.poll() is None:
            process.terminate()

    def check_cancelled(self):
        if self.cancelled:
            raise Cancelled()

    # -- persistence ---------------------------------------------------------
    def summary(self):  # one history row
        headline = ""
        for event in reversed(self.events):
            if event["type"] == "answer" and event.get("claims"):
                headline = event["claims"][0]["text"]
                break
            if event["type"] in ("message", "error"):
                headline = event.get("text") or event.get("message", "")
                break
        return {"id": self.id, "skill": self.skill, "question": self.request.get("question", ""),
                "status": self.status, "started": self.started,
                "seconds": round((self.finished or time.time()) - self.started, 2), "headline": headline[:200]}

    def to_json(self):
        return {"summary": self.summary(), "request": self.request, "events": self.events}


class StoredRun:  # a finished run loaded from disk; offers the same read API as Run
    def __init__(self, data):
        self.data, self.events, self.status = data, data["events"], data["summary"]["status"]
        self.id = data["summary"]["id"]

    def events_since(self, seq, timeout=0):
        return self.events[seq:], True

    def summary(self):
        return self.data["summary"]

    def to_json(self):
        return self.data

    def cancel(self):
        pass


class RunManager:
    def __init__(self, skills, data_dir=DATA):
        self.skills, self.data_dir = skills, Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs, self.active, self.lock = {}, None, threading.Lock()

    def start(self, skill_name, request):
        skill = self.skills.get(skill_name)
        if not skill:
            raise ValueError(f"Unknown skill: {skill_name}")
        request = skill.validate(request)  # reject bad input before anything starts
        with self.lock:
            if self.active and self.active.status == "running":
                raise Busy(f"Run {self.active.id} is still running")
            run = Run(skill_name, request)
            self.runs[run.id], self.active = run, run
        threading.Thread(target=self._execute, args=(skill, run), daemon=True).start()
        return run

    def _execute(self, skill, run):
        run.emit("run_start", run_id=run.id, skill=skill.name, request=run.request, stages=skill.stages(run.request))
        try:
            skill.run(run.request, run)
            status = "cancelled" if run.cancelled else "done"
        except Cancelled:
            status = "cancelled"
        except Exception as error:  # a failed stage or check; the message is safe to show (no secrets)
            status = "cancelled" if run.cancelled else "error"
            if status == "error":
                run.emit("error", message=str(error))
        run.finished = time.time()
        with run.cond:  # status and run_end together, so viewers never miss the final event
            run.status = status
        run.emit("run_end", status=status, seconds=round(run.finished - run.started, 2))
        self._save(run)

    def _save(self, run):
        path = self.data_dir / f"{run.id}.json"
        path.write_text(json.dumps(run.to_json(), ensure_ascii=False), encoding="utf-8")

    def get(self, run_id):
        if run_id in self.runs:
            return self.runs[run_id]
        path = self.data_dir / f"{run_id}.json"
        if "/" in run_id or "\\" in run_id or ".." in run_id or not path.exists():  # IDs only, never paths
            return None
        return StoredRun(json.loads(path.read_text(encoding="utf-8")))

    def history(self, limit=50):
        rows = {run.id: run.summary() for run in self.runs.values()}
        for path in sorted(self.data_dir.glob("*.json"), reverse=True)[:limit]:
            if path.stem not in rows:
                try:
                    rows[path.stem] = json.loads(path.read_text(encoding="utf-8"))["summary"]
                except (ValueError, KeyError):
                    continue  # skip a damaged file rather than breaking the whole list
        return sorted(rows.values(), key=lambda row: row["started"], reverse=True)[:limit]

    def active_id(self):
        return self.active.id if self.active and self.active.status == "running" else None
