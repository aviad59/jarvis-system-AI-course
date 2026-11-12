"""Skills: the things Jarvis can do. Each skill is one class; register it in SKILLS.

A skill declares:
  name, title, description  - shown in the console
  options                   - the console builds its controls from these (bool / int / select / text)
  stages(request)           - the steps the monitor shows before the run starts
  run(request, run)         - does the work, reporting progress with run.emit(...)

Event types the console understands (add new ones in app.js -> handlers):
  stage_start {stage, title}       stage_end {stage, status: ok|warning|error, seconds, code}
  log {stage, line}                retrieval {method, results:[...]}
  answer {claims, missing_information, sources, ...}
  message {text}                   status {systems}

To add a skill: subclass Skill, implement stages() and run(), and add an instance to SKILLS.
"""
import json  # read pipeline output files
import os  # platform check for venv paths
import sys  # Python executable for child processes
import tempfile  # MCP tool result file
import time  # stage timing
from pathlib import Path  # paths to the MCP server
import rag  # the RAG pipeline steps and paths
import router  # which skill a free-form request belongs to
import status  # system health checks

MAX_QUESTION = 2000
LAST_INDEX = {"documents": "vault", "source": "local"}  # what the last question used; the vault watcher re-indexes the same way


class Skill:
    name = title = description = ""
    options = []  # [{'key', 'label', 'type': 'bool'|'int'|'select'|'text', 'default', ...}]

    def validate(self, request):  # normalize request values using the declared options
        if not isinstance(request, dict):
            raise ValueError("Request must be an object")
        clean = {}
        for option in self.options:
            value = request.get(option["key"], option.get("default"))
            if option["type"] == "bool":
                value = bool(value)
            elif option["type"] == "int":
                value = int(value)
                if not option.get("min", value) <= value <= option.get("max", value):
                    raise ValueError(f"{option['label']} must be between {option['min']} and {option['max']}")
            elif option["type"] == "select" and value not in option["choices"]:
                raise ValueError(f"{option['label']} must be one of {option['choices']}")
            elif option["type"] == "text":
                value = str(value or "").strip()[:200]
            clean[option["key"]] = value
        return clean

    def stages(self, request):
        return []

    def run(self, request, run):
        raise NotImplementedError


class RagSkill(Skill):
    name, title = "rag", "Ask the documents"
    description = "Index the Obsidian vault and/or course documents → retrieve → answer with citations."
    needs_question = True
    options = [
        {"key": "documents", "label": "Documents", "type": "select", "choices": list(rag.DOCUMENT_SOURCES), "default": "vault"},
        {"key": "provider", "label": "Answer with", "type": "select", "choices": ["ollama", "anthropic"], "default": "ollama"},
        {"key": "model", "label": "Model", "type": "text", "default": "", "placeholder": "default for provider"},
        {"key": "source", "label": "Vectors from", "type": "select", "choices": ["local", "mongodb"], "default": "local"},
        {"key": "hybrid", "label": "Hybrid (BM25 + RRF)", "type": "bool", "default": False},
        {"key": "skip_index", "label": "Skip indexing", "type": "bool", "default": True},
        {"key": "exclude_superseded", "label": "Exclude superseded", "type": "bool", "default": False},
        {"key": "top_k", "label": "Top-k", "type": "int", "default": 3, "min": 1, "max": 10},
    ]

    def validate(self, request):
        clean = super().validate(request)
        question = str(request.get("question", "")).strip()
        if not question:
            raise ValueError("Ask a question first")
        if len(question) > MAX_QUESTION:
            raise ValueError(f"Question is longer than {MAX_QUESTION} characters")
        clean["question"] = question
        return clean

    def steps(self, request):
        return rag.build_steps(request["question"], skip_index=request["skip_index"], hybrid=request["hybrid"],
                               top_k=request["top_k"], exclude_superseded=request["exclude_superseded"],
                               provider=request["provider"], model=request["model"] or None, source=request["source"],
                               documents=request["documents"])

    def stages(self, request):
        return [{"id": step["id"], "title": step["title"]} for step in self.steps(request)]

    def run(self, request, run):
        LAST_INDEX.update(documents=request["documents"], source=request["source"])
        env = {}
        if request["provider"] == "anthropic":  # fail before indexing, not at the last stage
            key, _ = rag.find_anthropic_key()
            if not key:
                raise ValueError("ANTHROPIC_API_KEY is not set. Add it to a .env file next to rag.py, or choose the ollama provider")
            env["ANTHROPIC_API_KEY"] = key  # passed to the child only; never emitted
        for step in self.steps(request):
            run.emit("stage_start", stage=step["id"], title=step["title"])
            started = time.time()
            code = run.run_process(step["id"], [sys.executable, *step["args"]], step["folder"], env)
            run.check_cancelled()
            # run.py exits 2 when the answer cites a source that wasn't sent: the answer is saved, but flagged
            stage_status = "ok" if code == 0 else "warning" if step["id"] == "answer" and code == 2 else "error"
            run.emit("stage_end", stage=step["id"], status=stage_status, code=code, seconds=round(time.time() - started, 2))
            if stage_status == "error":  # surface the script's own STOP/ERROR line, which says what to fix
                reason = next((e["line"] for e in reversed(run.events) if e["type"] == "log" and e["stage"] == step["id"]
                               and e["line"].startswith(("STOP:", "ERROR:"))), None)
                raise RuntimeError(f"{step['title']} failed — " + (reason or f"exit code {code}, see the log"))
            if step["id"] == "retrieve":
                self.emit_retrieval(run)
            if step["id"] == "answer":
                self.emit_answer(run)

    @staticmethod
    def emit_retrieval(run):
        data = json.loads(rag.SEARCH_RESULTS.read_text(encoding="utf-8"))
        rows = [{"rank": r["rank"], "chunk_id": r["chunk_id"], "score": r["score"],
                 "semantic_score": r.get("semantic_score"), "bm25_score": r.get("bm25_score"),
                 "document_id": r["metadata"].get("document_id"), "version": r["metadata"].get("version"),
                 "status": r["metadata"].get("status"), "title": r["metadata"].get("title"),
                 "source": r["metadata"].get("source"), "meeting_date": r["metadata"].get("meeting_date"),
                 "project_id": r["metadata"].get("project_id")} for r in data["results"]]
        run.emit("retrieval", method=data["retrieval"]["method"], candidates=data["retrieval"].get("candidate_count"),
                 results=rows)

    @staticmethod
    def emit_answer(run):
        report = json.loads(rag.ANSWER.read_text(encoding="utf-8"))
        run.emit("answer", claims=report["answer"]["claims"], missing_information=report["answer"]["missing_information"],
                 sources=report["context"]["sources"], citation_status=report["citation_status"],
                 invalid_source_ids=report["invalid_source_ids"], provider=report["provider"], model=report["model"],
                 seconds=report["seconds"])


class VaultSyncSkill(Skill):  # started by the vault watcher (vault_watch.py) when notes change; also runnable by hand
    name, title = "vault_sync", "Sync vault"
    description = "Re-index the Obsidian vault now (only new or changed notes are embedded)."
    needs_question = False
    options = [
        {"key": "documents", "label": "Documents", "type": "select", "choices": ["vault", "both"], "default": "vault"},
        {"key": "source", "label": "Vectors to", "type": "select", "choices": ["local", "mongodb"], "default": "local"},
    ]

    def stages(self, request):
        return [{"id": s["id"], "title": s["title"]} for s in rag.index_steps(request["documents"], request["source"])]

    def run(self, request, run):
        for step in rag.index_steps(request["documents"], request["source"]):
            run.emit("stage_start", stage=step["id"], title=step["title"])
            started = time.time()
            code = run.run_process(step["id"], [sys.executable, *step["args"]], step["folder"], {})
            run.check_cancelled()
            run.emit("stage_end", stage=step["id"], status="ok" if code == 0 else "error", code=code,
                     seconds=round(time.time() - started, 2))
            if code != 0:
                reason = next((e["line"] for e in reversed(run.events) if e["type"] == "log" and e["stage"] == step["id"]
                               and e["line"].startswith(("STOP:", "ERROR:"))), None)
                raise RuntimeError(f"{step['title']} failed — " + (reason or f"exit code {code}, see the log"))
        notes = len(rag._vault_support().vault_signature(rag.VAULT))
        run.emit("message", text=f"Vault synced: {notes} notes are in the index.")


class StatusSkill(Skill):
    name, title = "status", "System check"
    description = "Check Ollama, MongoDB Atlas, the Anthropic key, the Obsidian vault, the local index and Whisper."
    needs_question = False

    def stages(self, request):
        return [{"id": "checks", "title": "System checks"}]

    def run(self, request, run):
        run.emit("stage_start", stage="checks", title="System checks")
        started = time.time()
        result = status.check_all(force=True)
        systems = result["systems"]
        bad = [s["label"] for s in systems.values() if not s["ok"]]
        run.emit("stage_end", stage="checks", status="warning" if bad else "ok", seconds=round(time.time() - started, 2))
        run.emit("status", systems=systems)
        run.emit("message", text=("All systems are ready." if not bad else "Needs attention: " + ", ".join(bad) + "."))


class ShortsSkill(Skill):  # Jarvis as an MCP client: calls the free youtube-shorts MCP server (social-mcp/) at runtime
    name, title = "shorts", "YouTube Shorts"
    description = "Fetch public YouTube Shorts through the youtube-shorts MCP server (free, no login). " \
                  "Type a search query, or a channel like @MrBeast."
    needs_question = True
    SOCIAL_MCP = Path(__file__).resolve().parents[3] / "social-mcp"  # repo root / social-mcp
    options = [
        {"key": "mode", "label": "Find by", "type": "select", "choices": ["search", "channel", "url"], "default": "search"},
        {"key": "limit", "label": "Results", "type": "int", "default": 5, "min": 1, "max": 25},
        {"key": "details", "label": "Likes, comments, date (slower)", "type": "bool", "default": False},
    ]

    def validate(self, request):
        clean = super().validate(request)
        query = str(request.get("question", "")).strip()[:200]
        if not query:
            raise ValueError("Type a search query or a channel (e.g. @MrBeast)")
        clean["question"] = query
        return clean

    def stages(self, request):
        return [{"id": "mcp", "title": "MCP · youtube-shorts"}]

    def python(self):  # the MCP server's own venv (fastmcp + yt-dlp); Jarvis's Python needs neither
        venv = self.SOCIAL_MCP / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        return str(venv) if venv.exists() else sys.executable

    def run(self, request, run):
        if request["mode"] == "url":
            tool, arguments = "get_post_details", {"url": request["question"]}
        else:
            if request["mode"] == "channel":
                tool, arguments = "get_youtube_shorts", {"channel": request["question"]}
            else:
                tool, arguments = "search_youtube_shorts", {"query": request["question"]}
            arguments.update(limit=request["limit"], details=request["details"])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "result.json"
            run.emit("stage_start", stage="mcp", title="MCP · youtube-shorts")
            started = time.time()
            code = run.run_process("mcp", [self.python(), str(self.SOCIAL_MCP / "jarvis_client.py"), "--tool", tool,
                                           "--args", json.dumps(arguments), "--output", str(output)],
                                   str(self.SOCIAL_MCP), {})
            run.check_cancelled()
            run.emit("stage_end", stage="mcp", status="ok" if code == 0 else "error", code=code,
                     seconds=round(time.time() - started, 2))
            if code != 0:
                reason = next((e["line"] for e in reversed(run.events) if e["type"] == "log" and e["stage"] == "mcp"
                               and e["line"].startswith(("STOP:", "ERROR:"))), None)
                raise RuntimeError("YouTube Shorts MCP call failed — " + (reason or f"exit code {code}, see the log"))
            data = json.loads(output.read_text(encoding="utf-8"))
        posts = data.get("posts") or ([data["post"]] if data.get("post") else [])  # get_post_details returns one post
        run.emit("posts", tool=tool, query=request["question"], posts=posts)


class JarvisSkill(Skill):  # one input for everything: router.py picks the skill, then that skill runs as usual
    name, title = "jarvis", "Ask Jarvis"
    description = ("Ask anything: questions go to your documents, YouTube requests (\"find shorts about cooking\", "
                   "@channel, a YouTube link) go to the youtube-shorts MCP server, \"system check\" checks the systems. "
                   "Start with /docs or /shorts to choose yourself.")
    needs_question = True

    @property
    def options(self):  # the documents options plus the Shorts ones; each target reads only its own keys
        shorts = [dict(o, label="Shorts: " + o["label"].lower()) for o in SKILLS["shorts"].options if o["key"] != "mode"]
        return SKILLS["rag"].options + shorts

    def validate(self, request):
        clean = super().validate(request)
        question = str(request.get("question", "")).strip()
        if not question:
            raise ValueError("Ask a question or make a request first")
        if len(question) > MAX_QUESTION:
            raise ValueError(f"Request is longer than {MAX_QUESTION} characters")
        target = router.route(question)
        if target.skill == "shorts" and not target.question:
            raise ValueError("What should I look for on YouTube? e.g. \"find shorts about cooking\" or \"@MrBeast\"")
        skill = SKILLS[target.skill]
        inner = skill.validate({**clean, **target.options, "question": target.question})  # rejects bad input up front
        return {**clean, "question": question,
                "route": {"skill": target.skill, "reason": target.reason, "title": skill.title, "request": inner}}

    def stages(self, request):
        target = request["route"]
        return [{"id": "route", "title": "Understand request"}, *SKILLS[target["skill"]].stages(target["request"])]

    def run(self, request, run):
        target = request["route"]
        run.emit("stage_start", stage="route", title="Understand request")
        run.emit("log", stage="route", line=f"→ {target['title']}: {target['reason']}")
        run.emit("route", skill=target["skill"], title=target["title"], reason=target["reason"])
        run.emit("stage_end", stage="route", status="ok", seconds=0)
        SKILLS[target["skill"]].run(target["request"], run)


SKILLS = {skill.name: skill for skill in (JarvisSkill(), RagSkill(), ShortsSkill(), VaultSyncSkill(), StatusSkill())}


def describe():  # what the console needs to draw the skill picker and its options
    return [{"name": s.name, "title": s.title, "description": s.description, "options": s.options,
             "needs_question": getattr(s, "needs_question", True)} for s in SKILLS.values()]
