"""Runs: one execution of a skill, recorded as an ordered list of events.

Every event is a dict {'seq', 'type', 'time', ...}. The browser follows a run live over
Server-Sent Events and can reconnect from any seq; finished runs are saved to data/runs/
so the history survives a server restart.

Feedback: after a run ends, the console lets you rate the response and tick what went wrong
(slow, wrong behavior, ...). It is stored on the run (summary.feedback) and appended to
data/runs/feedback.jsonl, one line per report with the run's context, for review later.
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
FEEDBACK_ISSUES = {  # id -> label shown as a tick box in the console
    "slow": "Slow response",
    "wrong_behavior": "Wrong behavior (did something I didn't ask for)",
    "misunderstood": "Misunderstood the request / wrong skill",
    "incorrect": "Incorrect or made-up answer",
    "incomplete": "Incomplete or missing information",
    "citations": "Bad or missing sources",
    "language": "Wrong language",
    "voice": "Voice problem (recognition or speech)",
    "media": "Wrong video or meme",
    "other": "Other",
}
MAX_FEEDBACK_COMMENT = 1000


def validate_feedback(body):  # {rating?, issues?, comment?} -> clean dict; any issue or comment means a 'bad' rating
    if not isinstance(body, dict):
        raise ValueError("Feedback must be a JSON object")
    issues = body.get("issues") or []
    if not isinstance(issues, list) or any(i not in FEEDBACK_ISSUES for i in issues):
        raise ValueError(f"Unknown feedback issue (known: {', '.join(FEEDBACK_ISSUES)})")
    comment = str(body.get("comment") or "").strip()
    if len(comment) > MAX_FEEDBACK_COMMENT:
        raise ValueError(f"Comment is longer than {MAX_FEEDBACK_COMMENT} characters")
    rating = body.get("rating") or ("bad" if issues or comment else None)
    if rating not in ("good", "bad"):
        raise ValueError("Rating must be 'good' or 'bad'")
    if rating == "good" and issues:
        raise ValueError("A 'good' rating can't have issues")
    return {"rating": rating, "issues": [i for i in FEEDBACK_ISSUES if i in issues],  # known order, no duplicates
            "comment": comment, "time": round(time.time(), 3)}


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
        self.feedback = None  # set by RunManager.set_feedback once the run has ended

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
                "seconds": round((self.finished or time.time()) - self.started, 2), "headline": headline[:200],
                "feedback": self.feedback}

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
    YIELDING = {"vault_sync"}  # background runs that a user's run waits for instead of getting Busy
    def __init__(self, skills, data_dir=DATA):
        self.skills, self.data_dir = skills, Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs, self.active, self.lock = {}, None, threading.Lock()
        self.feedback_log = self.data_dir / "feedback.jsonl"  # .jsonl: not picked up as a run file

    def start(self, skill_name, request):
        skill = self.skills.get(skill_name)
        if not skill:
            raise ValueError(f"Unknown skill: {skill_name}")
        request = skill.validate(request)  # reject bad input before anything starts
        background = self.active
        if background and background.skill in self.YIELDING and background.status == "running" and skill_name not in self.YIELDING:
            deadline = time.time() + 300  # a vault sync is short: let the question wait for it instead of failing
            while background.status == "running" and time.time() < deadline:
                background.events_since(len(background.events), timeout=1)
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

    def set_feedback(self, run_id, body):  # rate a finished run; a new report replaces the run's previous one
        run = self.get(run_id)
        if not run:
            return None
        if run.status == "running":
            raise ValueError("The run is still going: rate it when it ends")
        feedback = validate_feedback(body)
        with self.lock:
            if isinstance(run, StoredRun):
                run.data["summary"]["feedback"] = feedback
            else:
                run.feedback = feedback
            data = run.to_json()
            (self.data_dir / f"{run.id}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            answer = next((e for e in reversed(data["events"]) if e["type"] == "answer"), {})
            summary = data["summary"]
            row = {"run_id": run.id, **{k: summary.get(k) for k in ("skill", "question", "status", "seconds", "headline")},
                   "route": next((e.get("skill") for e in data["events"] if e["type"] == "route"), None),
                   "provider": answer.get("provider"), "model": answer.get("model"), **feedback}
            with self.feedback_log.open("a", encoding="utf-8") as log:
                log.write(json.dumps(row, ensure_ascii=False) + "\n")
        return feedback

    def feedback_reports(self, limit=50):  # newest first
        if not self.feedback_log.exists():
            return []
        rows = []
        for line in self.feedback_log.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows[::-1]

    def active_id(self):
        return self.active.id if self.active and self.active.status == "running" else None
