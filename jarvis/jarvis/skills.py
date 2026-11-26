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
import datetime  # when a judgement was saved
import json  # read pipeline output files
import os  # JARVIS_ROUTER
import runs  # log line limit
import sys  # Python executable for child processes
import re  # tidy a search query
import mcp_client  # one shared, long-lived connection per MCP server (.mcp.json)
import judge  # LLM as a judge: criteria, prompt, scores
import urllib.request  # ask the answer model for a search query
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
    listed = True  # shown in the console's skill picker
    manager = None  # the RunManager (set by it): lets a skill read other runs

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
        {"key": "provider", "label": "Answer with", "type": "select", "choices": list(rag.PROVIDERS), "default": "ollama"},
        {"key": "model", "label": "Model", "type": "text", "default": "", "placeholder": "default for provider"},
        {"key": "source", "label": "Vectors from", "type": "select", "choices": ["local", "mongodb"], "default": "local"},
        {"key": "hybrid", "label": "Hybrid (BM25 + RRF)", "type": "bool", "default": False},
        {"key": "skip_index", "label": "Skip indexing", "type": "bool", "default": True},
        {"key": "exclude_superseded", "label": "Exclude superseded", "type": "bool", "default": False},
        {"key": "top_k", "label": "Top-k", "type": "int", "default": 3, "min": 1, "max": 10},
        {"key": "self_query", "label": "Chat filters (self-query)", "type": "bool", "default": True},
    ]

    @staticmethod
    def filtering(request):  # extract sender/time filters first? Only MongoDB can filter inside the vector search
        return bool(request.get("self_query")) and request.get("source") == "mongodb"

    def validate(self, request):
        clean = super().validate(request)
        question = str(request.get("question", "")).strip()
        if not question:
            raise ValueError("Ask a question first")
        if len(question) > MAX_QUESTION:
            raise ValueError(f"Question is longer than {MAX_QUESTION} characters")
        clean["question"] = question
        clean["history"] = clean_history(request.get("history"))
        clean["rewrite"] = needs_rewrite(question, clean["history"])
        return clean

    def steps(self, request):
        return rag.build_steps(request["question"], skip_index=request["skip_index"], hybrid=request["hybrid"],
                               top_k=request["top_k"], exclude_superseded=request["exclude_superseded"],
                               provider=request["provider"], model=request["model"] or None, source=request["source"],
                               documents=request["documents"], history_file=rag.HISTORY if request.get("history") else None,
                               senders=request.get("senders"), date_from=request.get("date_from"),
                               date_to=request.get("date_to"), query=request.get("query"))

    def stages(self, request):
        context = [{"id": "context", "title": "Understand follow-up"}] if request.get("rewrite") else []
        if self.filtering(request):
            context.append({"id": "self_query", "title": "Chat filters"})
        return context + [{"id": step["id"], "title": step["title"]} for step in self.steps(request)]

    def extract_filters(self, request, run):  # "what did Omer say about X last week?" -> query X, sender, date range
        run.emit("stage_start", stage="self_query", title="Chat filters")
        started, model = time.time(), request["model"] or rag.DEFAULT_MODELS[request["provider"]]
        outcome = "ok"
        try:
            found = rag.self_query(request["question"],
                                   lambda prompt, schema: ask_json(prompt, schema, request["provider"], model))
            for item in found["dropped"]:
                run.emit("log", stage="self_query", line=f"dropped {item['field']} “{item['value']}”: {item['reason']}")
                outcome = "warning"
            if found["filtered"]:
                when = " → ".join(v for v in (found["from"], found["to"]) if v)
                run.emit("log", stage="self_query", line=f"search “{found['query']}”"
                         + (f" · senders: {', '.join(found['senders'])}" if found["senders"] else "")
                         + (f" · time: {when}" if when else ""))
                request = {**request, "query": found["query"], "senders": found["senders"],
                           "date_from": found["from"] or None, "date_to": found["to"] or None}
            else:
                run.emit("log", stage="self_query", line="no sender or time in the question: searching it as asked")
        except Exception as error:  # no filters is always a safe answer: retrieval runs exactly as without self-query
            outcome = "warning"
            run.emit("log", stage="self_query", line=f"filter extraction failed ({type(error).__name__}), searching without filters")
        run.check_cancelled()
        run.emit("stage_end", stage="self_query", status=outcome, seconds=round(time.time() - started, 2))
        return request

    def resolve(self, request, run):  # "and Harel?" -> "what does Harel like to eat?", so retrieval finds the right note
        run.emit("stage_start", stage="context", title="Understand follow-up")
        started, model = time.time(), request["model"] or rag.DEFAULT_MODELS[request["provider"]]
        try:
            question = standalone_question(request["question"], request["history"], request["provider"], model)
            run.emit("log", stage="context", line=f"“{request['question']}” → “{question}”")
            outcome = "ok"
        except Exception as error:  # keep the question as asked; the answer model still sees the conversation
            question, outcome = request["question"], "warning"
            run.emit("log", stage="context", line=f"rewrite failed ({type(error).__name__}), searching the question as asked")
        run.check_cancelled()
        run.emit("stage_end", stage="context", status=outcome, seconds=round(time.time() - started, 2))
        return {**request, "question": question}

    def run(self, request, run):
        LAST_INDEX.update(documents=request["documents"], source=request["source"])
        if request.get("history"):  # the answer model sees the conversation (context only, not evidence)
            rag.HISTORY.parent.mkdir(parents=True, exist_ok=True)
            rag.HISTORY.write_text(json.dumps(request["history"], ensure_ascii=False), encoding="utf-8")
        if request.get("rewrite"):
            request = self.resolve(request, run)
        if self.filtering(request):
            request = self.extract_filters(request, run)
        env = rag.provider_env(request["provider"])  # fails before indexing, not at the last stage;
        # the key is passed to the child process only and never emitted
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
                 "project_id": r["metadata"].get("project_id"), "participants": r.get("participants"),
                 "start_ts": r.get("start_ts"), "end_ts": r.get("end_ts"),
                 "text": r.get("text", "")} for r in data["results"]]  # shown when an Evidence item is clicked
        if data["retrieval"].get("relaxed"):  # no chunk matched every filter: say what was dropped
            run.emit("log", stage="retrieve", line="no chat chunk matched the filters; retried without: "
                     + ", ".join(data["retrieval"]["relaxed"]))
        run.emit("retrieval", method=data["retrieval"]["method"], candidates=data["retrieval"].get("candidate_count"),
                 results=rows, filters=data["retrieval"].get("applied_filters"), relaxed=data["retrieval"].get("relaxed"))

    @staticmethod
    def emit_answer(run):
        report = json.loads(rag.ANSWER.read_text(encoding="utf-8"))
        run.emit("answer", claims=report["answer"]["claims"], missing_information=report["answer"]["missing_information"],
                 sources=report["context"]["sources"], citation_status=report["citation_status"],
                 invalid_source_ids=report["invalid_source_ids"], provider=report["provider"], model=report["model"],
                 seconds=report["seconds"], question=report["context"].get("question"))


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
    description = "Check Ollama, MongoDB Atlas, the Anthropic and Gemini keys, the Obsidian vault, the local index and Whisper."
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

    def run(self, request, run):
        if request["mode"] == "url":
            tool, arguments = "get_post_details", {"url": request["question"]}
        else:
            if request["mode"] == "channel":
                tool, arguments = "get_youtube_shorts", {"channel": request["question"]}
            else:
                tool, arguments = "search_youtube_shorts", {"query": request["question"]}
            arguments.update(limit=request["limit"], details=request["details"])
        run.emit("stage_start", stage="mcp", title="MCP · youtube-shorts")
        started = time.time()
        try:
            data = mcp_call(run, "mcp", "youtube-shorts", tool, arguments)
            data = data if isinstance(data, dict) else {}
            error = None if data.get("ok") else data.get("error") or "the tool returned no result"
        except mcp_client.McpError as failure:  # the server didn't start, crashed, timed out or refused the call
            data, error = {}, str(failure)
        if error:
            run.emit("log", stage="mcp", line=f"ERROR: {error}")
        run.emit("stage_end", stage="mcp", status="error" if error else "ok", code=1 if error else 0,
                 seconds=round(time.time() - started, 2))
        if error:
            raise RuntimeError(f"YouTube Shorts MCP call failed — ERROR: {error}")
        posts = data.get("posts") or ([data["post"]] if data.get("post") else [])  # get_post_details returns one post
        for post in posts:
            run.emit("log", stage="mcp", line=f"  • {post.get('title')}  ({post.get('url')})")
        run.emit("log", stage="mcp", line=f"MCP: {data.get('count', len(posts))} result(s)")
        run.emit("posts", tool=tool, query=request["question"], posts=posts)


def mcp_call(run, stage, server, tool, arguments, timeout=120):
    """One tool call over Jarvis's shared MCP connection (mcp_client.py), logged in the stage; returns the tool's JSON.

    The server's stderr and an "MCP: call ..." line go to the Mission log. Cancel stops the wait, not the server."""
    def log(line):  # may be called after the call returns (a late stderr line): only while the run is live
        if run.status == "running" and run.log_lines < runs.MAX_LOG_LINES:  # a chatty server can't fill memory
            run.log_lines += 1
            run.emit("log", stage=stage, line=line)
    try:
        data = mcp_client.call_tool(server, tool, arguments, log=log, cancelled=lambda: run.cancelled, timeout=timeout)
    except mcp_client.Cancelled:
        run.check_cancelled()
        raise
    run.check_cancelled()
    return data


QUERY_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                "additionalProperties": False}
QUERY_PROMPT = ("Turn an answer into a YouTube search.\n\n"
                "Example 1. Request: what does the team like to eat? find shorts about it. "
                "Answer: Dana loves sushi; Avi eats burgers. -> {{\"query\": \"sushi burgers\"}}\n"
                "Example 2. Request: מה הוחלט בישיבה? תמצא סרטונים על זה. "
                "Answer: הוחלט לעבור ל-Kubernetes. -> {{\"query\": \"Kubernetes\"}}\n\n"
                "Request: {request}\nAnswer:\n{answer}\n\n"
                "Search for the specific things the ANSWER names (foods, tools, places, topics), 1 to 5 words, in the "
                "language of the answer. Never repeat the request's own words (team, meeting, likes, eat, find, shorts, "
                "video). If the answer names nothing specific, return an empty query. Reply as JSON: {{\"query\": \"...\"}}")


GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"


def ask_json(prompt, schema, provider, model, timeout=90, temperature=0, num_predict=None, strict=True, max_tokens=300):
    """One small structured call to the answer model; returns the parsed JSON. ValueError if its API key is missing.

    strict=False (Ollama only): no "format" in the request, the JSON is read from the reply text. Any format turns off
    Ollama's prompt cache, so a long fixed prompt (the router's tool list) is read again on every call: on a CPU that
    is ~10 s for 700 tokens. The caller must check the values itself."""
    key = next(iter(rag.provider_env(provider).values()), "")
    if provider == "anthropic":
        url, headers = "https://api.anthropic.com/v1/messages", {"x-api-key": key, "anthropic-version": "2026-10-21"}
        payload = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}],
                   "output_config": {"format": {"type": "json_schema", "schema": schema}}}
    elif provider == "gemini":  # the key goes in a header, never in the URL
        url, headers = f"{GEMINI_API}/models/{model}:generateContent", {"x-goog-api-key": key}
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema,
                                        "temperature": temperature,
                                        "maxOutputTokens": 4096}}  # leaves room for thinking tokens, which count toward it
    else:
        url, headers = "http://localhost:11434/api/chat", {}
        num_predict = num_predict or (None if strict else 80)
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False,
                   "options": {"temperature": temperature, **({"num_predict": num_predict} if num_predict else {})}}
        if strict:
            payload["format"] = schema
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    call = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(call, timeout=timeout) as response:
        reply = json.loads(response.read().decode("utf-8"))
    if provider == "anthropic":
        text = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
    elif provider == "gemini":  # skip thought parts; a blocked or cut-off reply has no JSON and fails to parse below
        parts = ((reply.get("candidates") or [{}])[0].get("content") or {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    else:
        text = reply.get("message", {}).get("content", "")
    if not strict:  # free text: the first {...} in the reply (models sometimes wrap it in ```json fences)
        found = re.search(r"\{.*\}", text, re.S)
        text = found.group(0) if found else text
    return json.loads(text)


def search_query(request_text, claims, provider, model):  # the notes' answer -> a Shorts search query
    prompt = QUERY_PROMPT.format(request=request_text, answer="\n".join("- " + c for c in claims))
    return clean_query(ask_json(prompt, QUERY_SCHEMA, provider, model).get("query", ""))


# The conversation so far (sent by the console), so follow-ups ("and Harel?", "find shorts about it") make sense.
MAX_TURNS, MAX_TURN_CHARS = 6, 800
FOLLOW_UP = re.compile(r"(?:^|\s)(?:it|that|this|he|she|they|him|her|them|his|its|their|those|these|also|"
                       r"what\s+about|and|זה|זאת|זו|הוא|היא|הם|הן|שלו|שלה|שלהם|אותו|אותה|אותם|גם|עליו|עליה|עליהם|"
                       r"לו|לה|להם|ומה|ומי|ואיך|ומתי|ואיפה|ולמה|וכמה|ואיזה)(?=[\s?!.,]|$)", re.I)
REWRITE_SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"],
                  "additionalProperties": False}
REWRITE_PROMPT = ("Rewrite the LAST question so it stands alone, using the conversation to resolve words such as "
                  "'it', 'he', 'that', 'and X?'. Keep its language. Do not answer it. If it already stands alone, "
                  "return it unchanged.\n\nExample. Conversation: Q: מה עידן אוהב לאכול? A: עידן אוהב פיצות. "
                  "Last question: והראל? -> {{\"question\": \"מה הראל אוהב לאכול?\"}}\n\n"
                  "Conversation:\n{conversation}\n\nLast question: {question}\n\nReply as JSON: {{\"question\": \"...\"}}")


def clean_history(value):  # [{question, answer}] from the browser: strings only, recent turns only, bounded size
    turns = []
    for turn in value if isinstance(value, list) else []:
        if isinstance(turn, dict):
            q, a = (str(turn.get(k) or "").strip()[:MAX_TURN_CHARS] for k in ("question", "answer"))
            if q and a:
                turns.append({"question": q, "answer": a})
    return turns[-MAX_TURNS:]


def needs_rewrite(question, history):  # a follow-up that can't be searched on its own words
    return bool(history) and (len(question.split()) <= 2 or bool(FOLLOW_UP.search(question)))  # "והראל?", "and him?"


def standalone_question(question, history, provider, model):
    conversation = "\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in history)
    text = str(ask_json(REWRITE_PROMPT.format(conversation=conversation, question=question), REWRITE_SCHEMA,
                        provider, model).get("question") or "").strip()
    return text[:MAX_QUESTION] or question


def clean_query(text):
    text = re.sub(r"\[[^\]]*\]|\(S\d+\)|[\"“”]", " ", str(text or ""))  # citation marks, quotes
    return re.sub(r"\s+", " ", text).strip()[:100]


def fallback_query(claims):  # no model reply: the first claim, cut to a search-sized phrase
    return " ".join(clean_query(claims[0]).rstrip(".").split()[:8]) if claims else ""


# Does a YouTube topic name something from the notes ("find shorts about Idan")? Then the notes answer first.
HEBREW_PREFIXES = "והבכלמש"  # "ועידן", "לעידן", "שעידן" -> "עידן"
COMMON = {"the", "and", "for", "with", "about", "this", "that", "what", "how", "who", "של", "על", "עם", "את", "זה",
          "מה", "מי", "איך", "לי", "גם", "או", "אם", "כל", "יש", "אין", "הוא", "היא", "הם", "אני", "אתה", "שלי"}
_vault_words = {"snapshot": None, "words": set(), "sounds": set(), "pairs": set(), "frequent": set()}
FREQUENT_NAME = 5  # a short name ("Omer" -> "mr") counts alone only if the notes use it this often (a chat participant)
# Sound keys: consonants only, the same for both scripts ("ברנשטיין" / "Bernstein" -> "brnstn", "עומר" / "Omer" -> "mr")
HEBREW_SOUNDS = {"ב": "b", "ג": "g", "ד": "d", "ז": "z", "ט": "t", "כ": "k", "ך": "k", "ל": "l", "מ": "m", "ם": "m",
                 "נ": "n", "ן": "n", "ס": "s", "פ": "p", "ף": "p", "צ": "s", "ץ": "s", "ק": "k", "ר": "r", "ש": "s", "ת": "t"}
LATIN_SOUNDS = (("sch", "s"), ("sh", "s"), ("ch", "h"), ("ph", "p"), ("ck", "k"), ("tz", "s"), ("ts", "s"), ("th", "t"),
                ("w", "b"), ("v", "b"), ("f", "p"), ("c", "k"), ("q", "k"), ("x", "ks"), ("j", "g"))
NAME = re.compile(r"\b[A-Z][a-z]{1,20}\b")  # a capitalized Latin word: names in a chat export ("Omer Bernstein:")


def sound_key(word):
    word = word.lower()
    if re.search(r"[֐-׿]", word):
        key = "".join(HEBREW_SOUNDS.get(c, "") for c in word)  # א ה ו י ע ח: vowels / silent
    else:
        for a, b in LATIN_SOUNDS:
            word = word.replace(a, b)
        key = re.sub(r"[aeiouyh\W\d_]", "", word)
    return re.sub(r"(.)\1+", r"\1", key)


def _forms(word):  # a word and its forms without up to two Hebrew prefix letters ("ולעידן" -> "לעידן", "עידן")
    word = word.lower()
    forms = {word}
    for cut in (1, 2):
        if len(word) - cut >= 3 and all(c in HEBREW_PREFIXES for c in word[:cut]):
            forms.add(word[cut:])
    return forms


def vault_words():  # every word in the notes, cached until a note changes
    support = rag._vault_support()
    snapshot = support.vault_snapshot(rag.VAULT)
    if snapshot is None:
        return set()
    if snapshot != _vault_words["snapshot"]:
        files, _ = support.vault_files(rag.VAULT)
        words, sounds, pairs, counts = set(), set(), set(), {}
        for doc in support._read_vault(rag.VAULT, files, quiet=True):
            text = f"{doc.get('title', '')} {doc.get('body', '')} {doc.get('path', '')}"
            for word in re.findall(r"\w{2,}", text):
                words |= _forms(word)
            for line in text.splitlines():
                names = [sound_key(n) for n in NAME.findall(line)]
                sounds.update(k for k in names if len(k) >= 3)
                for k in names:
                    counts[k] = counts.get(k, 0) + 1
                pairs.update(f"{a} {b}" for a, b in zip(names, names[1:]) if a and b)  # "Omer Bernstein" -> "mr brnstn"
        frequent = {k for k, n in counts.items() if len(k) >= 2 and n >= FREQUENT_NAME}
        _vault_words.update(snapshot=snapshot, words=words - COMMON, sounds=sounds, pairs=pairs, frequent=frequent)
    return _vault_words["words"]


def vault_mentions(topic):  # the topic's words that also appear in the notes
    try:
        known = vault_words()
    except Exception:  # vault missing or unreadable: plain YouTube search
        return []
    words = [w for w in re.findall(r"\w{2,}", topic) if w.lower() not in COMMON]
    found = [w for w in words if _forms(w) & known]
    hebrew = [w for w in words if re.search(r"[֐-׿]", w)]  # Hebrew request, names written in English
    keys = [sound_key(w) for w in hebrew]
    for i, (word, key) in enumerate(zip(hebrew, keys)):
        stripped = {sound_key(f) for f in _forms(word)}
        if word not in found and (any(len(k) >= 4 and k in _vault_words["sounds"] or k in _vault_words["frequent"]
                                      for k in stripped)
                                  or (i + 1 < len(keys) and f"{key} {keys[i + 1]}" in _vault_words["pairs"])
                                  or (i > 0 and f"{keys[i - 1]} {key}" in _vault_words["pairs"])):
            found.append(word)
    return found


MEME_PROMPT = ("You write memes. Topic: {request}\n\n"
               "{notes}"
               "{choice}\n\n"
               "Write exactly {count} short, funny captions (at most 7 words each), in order (top, then bottom), "
               "in {language}. The joke is about the topic{grounded}. Reply as JSON: {{\"template_id\": \"...\", \"lines\": [...]}}")
NOTES_FOR_MEME = ("What the user's own notes say about it (real people, events and details; base the joke on these "
                  "facts, and ignore any that are unrelated to the topic):\n{facts}\n\n")
MEME_NOTES_CHARS = 1500  # total excerpt size: enough facts for a joke, small enough for a local model
# Well-known templates with two captions (top / bottom): a short list keeps a small local model fast and on target.
MEME_SHORTLIST = ("drake", "fry", "buzz", "doge", "fine", "rollsafe", "success", "spongebob", "woman-cat", "harold",
                  "kermit", "both", "yuno", "aag", "ackbar", "afraid", "blb", "captain", "cheems", "disastergirl",
                  "grumpycat", "interesting", "khaby-lame", "morpheus", "mordor", "officespace", "philosoraptor", "pooh",
                  "scc", "stonks", "wonka", "xy", "yodawg", "toohigh", "worst")
MEME_WORDS = re.compile(r"\b(?:you|your|tell|say|know|any|got|jokes?|laugh|cheer|up|us|something|"
                        r"make|create|generate|build|draw|do|give|show|send|me|a|an|the|some|funny|new|please|"
                        r"memes?|about|of|on|for|with|template)\b|(?:^|\s)(?:תעשה|תכין|צור|תיצור|תייצר|עשה|הכן|תן|תביא|"
                        r"תראה|תמציא|לי|מם|מימ|ממים|מימים|מימז|לימים|לממים|למם|מצחיק|מצחיקים|על|בבקשה|של|עם|ספר|תספר|"
                        r"תספרי|תגיד|תגידי|יש|לך|בדיחה|הבדיחה|בדיחות|תצחיק|תצחיקי|אותי|אותנו|משהו|תשמח|לימימ|לימז|"
                        r"לימימז|מימס|ממז|מים|המים)(?=[\s?!.,:]|$)", re.I)


def _meme_subject(text):  # "make me a funny meme about monday meetings" -> "monday meetings"; "" when there is none
    return re.sub(r"\s+", " ", MEME_WORDS.sub(" ", text)).strip(" :,.!?")


def _meme_topic(text):  # the subject, or the whole request ("tell me a joke": the model picks the joke)
    return _meme_subject(text) or text


def meme_captions(text):
    """Captions written by the user: after a colon, split on / | or new lines; or "quoted" "parts"."""
    quoted = re.findall(r"[\"“]([^\"”]+)[\"”]", text)
    if quoted:
        return [q.strip() for q in quoted if q.strip()]
    if ":" not in text:
        return []
    after = text.split(":", 1)[1]
    lines = [part.strip() for part in re.split(r"\s*(?:/|\||\n)\s*", after) if part.strip()]
    return lines


def meme_template(text, templates):
    """A template the user named: "drake meme", "meme of fry", "template buzz", or a full name ("futurama fry")."""
    lowered = text.lower()
    by_id = {t["id"]: t for t in templates}
    for pattern in (r"\b([a-z0-9][a-z0-9_-]*)\s+(?:memes?|template)\b", r"\b(?:template|memes?\s+(?:of|with))\s+([a-z0-9][a-z0-9_-]*)\b"):
        for word in re.findall(pattern, lowered):
            if word in by_id:
                return word
    named = [t for t in templates if len(t["name"]) > 4 and t["name"].lower() in lowered]
    return max(named, key=lambda t: len(t["name"]))["id"] if named else None


class NotesFirst(Skill):  # a skill that first searches the notes for its topic (retrieval only, no RAG answer)
    noun = "result"  # "meme", "song": for the log line when the search fails

    def note_steps(self, request):  # the RAG pipeline up to retrieval: index if stale, then search
        return [step for step in SKILLS["rag"].steps(request["notes"]) if step["id"] != "answer"] if request.get("notes") else []

    def search_notes(self, request, run):
        """Search the notes for the topic; returns [(title, excerpt)]. A failure only means working without notes."""
        LAST_INDEX.update(documents=request["notes"]["documents"], source=request["notes"]["source"])
        for step in self.note_steps(request):
            run.emit("stage_start", stage=step["id"], title="Notes · " + step["title"])
            started = time.time()
            code = run.run_process(step["id"], [sys.executable, *step["args"]], step["folder"], {})
            run.check_cancelled()
            run.emit("stage_end", stage=step["id"], status="ok" if code == 0 else "warning", code=code,
                     seconds=round(time.time() - started, 2))
            if code != 0:
                run.emit("log", stage=step["id"], line=f"notes search failed: making the {self.noun} from the topic alone")
                return []
            if step["id"] == "retrieve":
                RagSkill.emit_retrieval(run)  # the evidence panel shows what it is based on
        results = json.loads(rag.SEARCH_RESULTS.read_text(encoding="utf-8")).get("results", [])
        facts, room = [], MEME_NOTES_CHARS
        for r in results:
            excerpt = re.sub(r"\s+", " ", r.get("text", "")).strip()[:min(500, room)]
            if excerpt:
                facts.append((r["metadata"].get("title") or r["metadata"].get("source") or "note", excerpt))
                room -= len(excerpt)
            if room <= 0:
                break
        return facts


class MemeSkill(NotesFirst):  # Jarvis as an MCP client: plans a meme, then calls the memes MCP server (meme-mcp/)
    name, title, noun = "meme", "Memes", "meme"
    description = ("Make a meme through the memes MCP server (open-source Memegen, free). Say what it's about, "
                   "or name a template and write the captions: \"drake meme: writing tests / shipping on friday\".")
    needs_question = True
    MEDIA = Path(__file__).resolve().parent / "data" / "media"  # locally drawn memes, served at /media/<name>
    options = [
        {"key": "template", "label": "Template", "type": "text", "default": "", "placeholder": "auto (e.g. drake)"},
        {"key": "captions_by", "label": "Captions by", "type": "select", "choices": list(rag.PROVIDERS), "default": "ollama"},
        {"key": "use_notes", "label": "Use my notes", "type": "bool", "default": True},
    ]
    _templates = {"list": None, "at": 0.0}

    def validate(self, request):
        clean = super().validate(request)
        text = str(request.get("question", "")).strip()[:500]
        if not text:
            raise ValueError("Say what the meme should be about, e.g. \"make a meme about monday meetings\"")
        if clean["template"] and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", clean["template"].lower()):
            raise ValueError("Template must be a Memegen template id, e.g. drake")
        clean["template"] = clean["template"].lower()
        clean["question"] = text
        # The joke comes from the notes: search them first (retrieval only, no RAG answer), unless the user already
        # wrote the captions. Uses the documents options (vault/course, local/MongoDB, top-k) like any question.
        clean["notes"] = None
        subject = _meme_subject(text)
        if clean["use_notes"] and subject and not meme_captions(text):  # "tell me a joke": nothing to look up
            notes = SKILLS["rag"].validate({**request, "question": subject, "history": None})
            clean["notes"] = {**notes, "rewrite": False}
        return clean

    def stages(self, request):
        notes = [{"id": s["id"], "title": "Notes · " + s["title"]} for s in self.note_steps(request)]
        return notes + [{"id": "plan", "title": "Plan the meme"}, {"id": "mcp", "title": "MCP · memes"}]

    def call(self, run, stage, tool, arguments):  # one call to the memes MCP server (shared connection); returns its JSON
        try:
            data = mcp_call(run, stage, "memes", tool, arguments)
            data = data if isinstance(data, dict) else {}
        except mcp_client.McpError as failure:  # e.g. an invalid template id: the server's own message
            data = {"ok": False, "error": str(failure)}
        if not data.get("ok"):
            error = data.get("error") or "the tool returned no result"
            run.emit("log", stage=stage, line=f"ERROR: {error}")
            raise RuntimeError(f"Memes MCP call failed — {error}")
        if data.get("url") or data.get("file"):
            run.emit("log", stage=stage, line=f"MCP: meme ready ({data.get('renderer')}): {data.get('url') or data.get('file')}")
        else:
            run.emit("log", stage=stage, line=f"MCP: {data.get('count', 0)} result(s)")
        return data

    def templates(self, run):
        if self._templates["list"] is None or time.time() - self._templates["at"] > 3600:
            data = self.call(run, "plan", "list_templates", {"limit": 300})
            self._templates.update(list=data["templates"], at=time.time())
        return self._templates["list"]

    def write(self, request, template, templates, provider, facts=()):  # the model picks a template (if needed), writes captions
        by_id = {t["id"]: t for t in templates}
        if template:
            count, ids = max(1, min(int(by_id[template]["lines"]), 6)), [template]
            choice = f"Use the template {template} ({by_id[template]['name']})."
        else:
            ids = [i for i in MEME_SHORTLIST if i in by_id] or ["drake"]
            count = 2
            choice = "Pick the template that fits the joke best (use its id):\n" + \
                     "\n".join(f"- {i}: {by_id[i]['name'] if i in by_id else i}" for i in ids)
        language = "Hebrew" if re.search(r"[֐-׿]", request) else "English"  # stated outright: small models drift
        notes = NOTES_FOR_MEME.format(facts="\n".join(f"- [{title}] {text}" for title, text in facts)) if facts else ""
        prompt = MEME_PROMPT.format(request=request, notes=notes, choice=choice, count=count, language=language,
                                    grounded=", using what the notes say" if facts else "")
        schema = {"type": "object", "additionalProperties": False, "required": ["template_id", "lines"],
                  "properties": {"template_id": {"type": "string", "enum": ids},
                                 "lines": {"type": "array", "items": {"type": "string"}, "minItems": count, "maxItems": count}}}
        model = rag.DEFAULT_MODELS[provider]
        plan = ask_json(prompt, schema, provider, model, timeout=120, temperature=0.8, num_predict=160)
        lines = [str(line).strip() for line in plan.get("lines", [])][:count]
        lines += [""] * (count - len(lines))  # one entry per text box, even if the model gave fewer
        if not any(lines):
            raise ValueError("The model wrote no captions; try again or write them: \"drake meme: top / bottom\"")
        chosen = template or str(plan.get("template_id", "")).strip().lower()
        if chosen not in ids:
            chosen = ids[0]
        return chosen, lines, f"{provider} · {model}"

    def run(self, request, run):
        text = request["question"]
        facts = self.search_notes(request, run) if request.get("notes") else []  # 1. what the notes say about it
        run.emit("stage_start", stage="plan", title="Plan the meme")
        started = time.time()
        templates = self.templates(run)
        known = {t["id"] for t in templates}
        if request["template"] and request["template"] not in known:
            raise ValueError(f"Unknown template {request['template']!r}; ask for \"list meme templates\" ideas, e.g. drake")
        template = request["template"] or meme_template(text, templates)
        lines = meme_captions(text)
        if template and lines:
            run.emit("log", stage="plan", line=f"template {template} and captions from your words (no model needed)")
        else:
            try:
                template, lines, by = self.write(_meme_topic(text) if not lines else text, template, templates,
                                                 request["captions_by"], facts)
                based = f" (from {len(facts)} note excerpt{'s' if len(facts) != 1 else ''})" if facts else ""
                run.emit("log", stage="plan", line=f"{by} → {template}: {' / '.join(lines)}{based}")
            except ValueError:
                raise
            except Exception as error:
                if not lines:
                    raise RuntimeError(f"Couldn't write captions ({type(error).__name__}: is {request['captions_by']} reachable?). "
                                       "Write them yourself: \"drake meme: top text / bottom text\"") from None
                template = template or ("drake" if len(lines) == 2 else "fry")
        run.check_cancelled()
        run.emit("stage_end", stage="plan", status="ok", seconds=round(time.time() - started, 2))
        run.emit("stage_start", stage="mcp", title="MCP · memes")
        started = time.time()
        data = self.call(run, "mcp", "generate_meme", {"template_id": template, "text_lines": lines})
        image = data.get("url")
        if data.get("file"):  # Hebrew: drawn locally by the MCP server; Jarvis serves a copy from its media folder
            source = Path(data["file"])
            if not re.fullmatch(r"[0-9a-f]{16}\.png", source.name) or not source.is_file():
                raise RuntimeError("The memes MCP server returned an unexpected file")
            self.MEDIA.mkdir(parents=True, exist_ok=True)
            (self.MEDIA / source.name).write_bytes(source.read_bytes())
            image = f"/media/{source.name}"
        run.emit("stage_end", stage="mcp", status="ok", seconds=round(time.time() - started, 2))
        run.emit("meme", image=image, template=template, name=data.get("name", template), text=lines,
                 renderer=data.get("renderer"), hebrew=bool(re.search(r"[֐-׿]", " ".join(lines) + text)),
                 notes=sorted({title for title, _ in facts}))


SONG_PROMPT = ("You write songs. Topic: {topic}\n\n"
               "{notes}"
               "Write a short, catchy song about the topic{grounded}, in {language}: a title, a style for the music "
               "(genre, mood, instruments: 2 to 8 words, in English) and the lyrics, with [Verse] and [Chorus] parts, "
               "at most 16 lines. Reply as JSON: {{\"title\": \"...\", \"style\": \"...\", \"lyrics\": \"...\"}}")
NOTES_FOR_SONG = ("What the user's own notes say about it (real people, events and details; write about these facts, "
                  "and ignore any that are unrelated to the topic):\n{facts}\n\n")
SONG_TIMEOUT = 600  # seconds for create_song: the music server waits for the songs itself (MUSIC_WAIT_SECONDS, 240 s)
# "style: lo-fi", "title: Monday", "lyrics: ..." (and in Hebrew) inside the request: used as given, no model needed
SONG_FIELDS = re.compile(r"(?:^|(?<=[\s,;.]))(style|genre|title|lyrics|סגנון|ז['׳]אנר|כותרת|שם\s+השיר|מילים)\s*:\s*", re.I)
FIELD_KEYS = {"style": "style", "genre": "style", "סגנון": "style", "ז'אנר": "style", "ז׳אנר": "style",
              "title": "title", "כותרת": "title", "שם השיר": "title", "lyrics": "lyrics", "מילים": "lyrics"}
INSTRUMENTAL = re.compile(r"\binstrumental\b|\bno\s+(?:vocals|words|lyrics)\b|\bwithout\s+(?:vocals|words|lyrics)\b|"
                          r"(?:^|\s)(?:אינסטרומנטלי|אינסטרומנטלית|בלי\s+מילים|ללא\s+מילים|בלי\s+שירה)(?=[\s?!.,:]|$)", re.I)
SONG_WORDS = re.compile(r"\b(?:make|create|generate|build|write|compose|sing|produce|record|do|give|send|can|could|would|"
                        r"you|i|want|need|me|us|a|an|the|some|please|new|funny|short|nice|cool|catchy|little|"
                        r"songs?|tracks?|tunes?|jingles?|anthems?|ballads?|lullaby|lullabies|raps?|music|about|of|on|for|with)\b|"
                        r"(?:^|\s)(?:תעשה|תעשי|תכין|תכיני|צור|תיצור|תייצר|עשה|הכן|תן|תני|תביא|תכתוב|תכתבי|תלחין|תלחיני|"
                        r"תשיר|תשירי|תחבר|תחברי|תפיק|לי|לנו|ה?שיר|ה?שירים|ג['׳]ינגל|ה?המנון|ה?ראפ|ה?מוזיקה|ה?מנגינה|ה?בלדה|"
                        r"על|בבקשה|של|עם|בנושא|קצר|קצרה|מצחיק|מצחיקה|חדש|חדשה|יפה)(?=[\s?!.,:]|$)", re.I)


def _song_subject(text):  # "make me a catchy song about monday meetings" -> "monday meetings"; "" when there is none
    return re.sub(r"\s+", " ", SONG_WORDS.sub(" ", SONG_WORDS.sub(" ", text))).strip(" :,.!?")


def song_parts(text):
    """The request -> {subject, style, title, lyrics, instrumental}: what the user wrote out, and what it's about."""
    pieces = SONG_FIELDS.split(text)
    parts = {"style": "", "title": "", "lyrics": ""}
    for label, value in zip(pieces[1::2], pieces[2::2]):
        key = FIELD_KEYS.get(re.sub(r"\s+", " ", label.lower()), "style")
        parts[key] = value.strip(" ,;\n")[:3000] if key == "lyrics" else " ".join(value.split()).strip(" ,;.")[:200]
    head = pieces[0]
    return {**parts, "instrumental": bool(INSTRUMENTAL.search(text)), "subject": _song_subject(INSTRUMENTAL.sub(" ", head))}


def _https_only(song):  # links from the MCP server go into <audio>/<img>/<a>: https only, whatever the server sent
    return {k: (v if not k.endswith("_url") or (isinstance(v, str) and v.startswith("https://")) else None)
            for k, v in song.items()}


SONG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")  # the music server checks them too


def check_songs(ids):
    """The song card's "Check again": the latest records of songs still being made -> {status, songs, message}."""
    ids = [str(one).strip() for one in (ids if isinstance(ids, list) else [])][:12]
    if not ids or not all(SONG_ID.match(one) for one in ids):
        raise ValueError("Give the ids of the songs to check")
    log = []  # not a run: the server's log lines go back with the answer, and the console adds them to the Mission log
    try:
        data = mcp_client.call_tool("music", "get_song", {"song_ids": ids}, timeout=150,
                                    log=lambda line: len(log) < 200 and log.append(line))
    except mcp_client.McpError as failure:
        raise RuntimeError(f"Music MCP call failed — {failure}") from None
    data = data if isinstance(data, dict) else {}
    if not data.get("ok"):
        raise RuntimeError(f"Music MCP call failed — {data.get('error') or 'the tool returned no result'}")
    return {"status": data.get("status"), "supplier": data.get("supplier"), "message": data.get("message") or "",
            "songs": [_https_only(s) for s in data.get("songs") or []][:12], "log": list(log)}


class SongSkill(NotesFirst):  # Jarvis as an MCP client: writes the song, then calls the music MCP server (music-mcp/)
    name, title, noun = "song", "Songs", "song"
    description = ("Make a song through the music MCP server (Suno's official MCP server by default; uses your Suno "
                   "credits). Say what it's about; add \"style: ...\", \"title: ...\" or your own \"lyrics: ...\" if you like.")
    needs_question = True
    options = [
        {"key": "song_style", "label": "Style", "type": "text", "default": "", "placeholder": "auto (e.g. lo-fi, rock)"},
        {"key": "lyrics_by", "label": "Lyrics by", "type": "select", "choices": [*rag.PROVIDERS, "music service"],
         "default": "ollama"},
        {"key": "song_notes", "label": "Use my notes", "type": "bool", "default": True},
        {"key": "instrumental", "label": "Instrumental", "type": "bool", "default": False},
    ]

    def validate(self, request):
        clean = super().validate(request)
        text = str(request.get("question", "")).strip()[:MAX_QUESTION]
        if not text:
            raise ValueError("Say what the song should be about, e.g. \"make a song about monday meetings\"")
        parts = song_parts(text)
        instrumental = clean["instrumental"] or parts["instrumental"]
        clean.update(question=text, parts=parts, instrumental=instrumental, notes=None)
        # The notes are searched only when a model writes the lyrics here: then only the topic and the finished lyrics
        # go to the music service, never the note excerpts themselves.
        writes = clean["lyrics_by"] in rag.PROVIDERS and not parts["lyrics"] and not instrumental
        if clean["song_notes"] and parts["subject"] and writes:
            notes = SKILLS["rag"].validate({**request, "question": parts["subject"], "history": None})
            clean["notes"] = {**notes, "rewrite": False}
        return clean

    def stages(self, request):
        notes = [{"id": s["id"], "title": "Notes · " + s["title"]} for s in self.note_steps(request)]
        return notes + [{"id": "plan", "title": "Write the song"}, {"id": "mcp", "title": "MCP · music"}]

    def call(self, run, stage, tool, arguments):  # one call to the music MCP server (shared connection); returns its JSON
        try:
            data = mcp_call(run, stage, "music", tool, arguments, timeout=SONG_TIMEOUT)
            data = data if isinstance(data, dict) else {}
        except mcp_client.McpError as failure:  # the server didn't start, crashed or timed out
            data = {"ok": False, "error": str(failure)}
        if not data.get("ok"):
            error = data.get("error") or "the tool returned no result"
            run.emit("log", stage=stage, line=f"ERROR: {error}")
            raise RuntimeError(f"Music MCP call failed — {error}")
        songs = data.get("songs") or []
        for song in songs:
            run.emit("log", stage=stage, line=f"  • {song.get('title') or song.get('id')}  ({song.get('audio_url') or song.get('status')})")
        run.emit("log", stage=stage, line=f"MCP: {data.get('status')} · {len(songs)} song(s) from {data.get('supplier')}")
        return data

    def write(self, topic, facts, provider):  # the answer model writes the title, style and lyrics, in the user's language
        language = "Hebrew" if re.search(r"[֐-׿]", topic) else "English"
        notes = NOTES_FOR_SONG.format(facts="\n".join(f"- [{title}] {text}" for title, text in facts)) if facts else ""
        prompt = SONG_PROMPT.format(topic=topic, notes=notes, language=language,
                                    grounded=", using what the notes say" if facts else "")
        schema = {"type": "object", "additionalProperties": False, "required": ["title", "style", "lyrics"],
                  "properties": {"title": {"type": "string"}, "style": {"type": "string"}, "lyrics": {"type": "string"}}}
        model = rag.DEFAULT_MODELS[provider]
        plan = ask_json(prompt, schema, provider, model, timeout=180, temperature=0.8, num_predict=700)
        lyrics = str(plan.get("lyrics") or "").strip()[:3000]
        if not lyrics:
            raise ValueError("the model wrote no lyrics")
        return ({"title": " ".join(str(plan.get("title") or "").split())[:100],
                 "style": " ".join(str(plan.get("style") or "").split())[:200], "lyrics": lyrics}, f"{provider} · {model}")

    def run(self, request, run):
        parts = request["parts"]
        facts = self.search_notes(request, run) if request.get("notes") else []  # 1. what the notes say about it
        run.emit("stage_start", stage="plan", title="Write the song")
        started, outcome = time.time(), "ok"
        topic = parts["subject"] or request["question"]
        song = {"description": topic, "lyrics": parts["lyrics"], "style": parts["style"] or request["song_style"],
                "title": parts["title"], "instrumental": request["instrumental"]}
        if parts["lyrics"]:
            run.emit("log", stage="plan", line="lyrics from your words (no model needed)")
        elif request["instrumental"]:
            run.emit("log", stage="plan", line="instrumental: no lyrics")
        elif request["lyrics_by"] in rag.PROVIDERS:
            try:
                written, by = self.write(topic, facts, request["lyrics_by"])
                song.update(lyrics=written["lyrics"], title=song["title"] or written["title"],
                            style=song["style"] or written["style"])
                based = f" (from {len(facts)} note excerpt{'s' if len(facts) != 1 else ''})" if facts else ""
                run.emit("log", stage="plan", line=f"{by} wrote “{song['title']}” · {song['style']}{based}")
            except Exception as error:  # the music service still writes lyrics from the topic
                outcome = "warning"
                run.emit("log", stage="plan", line=f"lyrics model failed ({type(error).__name__}: is {request['lyrics_by']} "
                                                   "reachable?): the music service writes them from the topic")
        else:
            run.emit("log", stage="plan", line="the music service writes the lyrics from the topic")
        run.check_cancelled()
        run.emit("stage_end", stage="plan", status=outcome, seconds=round(time.time() - started, 2))
        run.emit("stage_start", stage="mcp", title="MCP · music")
        started = time.time()
        data = self.call(run, "mcp", "create_song", {**song, "wait": True})
        run.emit("stage_end", stage="mcp", status="ok" if data.get("status") == "ready" else "warning",
                 seconds=round(time.time() - started, 2))
        run.emit("song", songs=[_https_only(s) for s in data.get("songs") or []][:4], status=data.get("status"),
                 supplier=data.get("supplier"), message=data.get("message") or "", title=song["title"],
                 style=song["style"], lyrics=song["lyrics"], topic=topic,
                 hebrew=bool(re.search(r"[֐-׿]", request["question"])), notes=sorted({title for title, _ in facts}))


def notes_question_about(topic):  # "עומר ברנשטיין" -> "מה כתוב בפתקים על עומר ברנשטיין?"
    hebrew = re.search(r"[\u0590-\u05FF]", topic)
    return f"מה כתוב בפתקים על {topic}?" if hebrew else f"What do the notes say about {topic}?"


ROUTER = os.environ.get("JARVIS_ROUTER", "rules")  # default of "Route with": rules | model (gemma3:4b on a CPU: ~17 s, ~70% right)
ROUTE_TIMEOUT = 60  # seconds; the first call may load the model


_mcp_tools = {}  # server -> {tool: description}, filled once in the background at startup (server.py warm_up)


def warm_up_mcp_tools():  # start each MCP server once and keep what its tools say about themselves, for the router
    import mcp_client  # the shared MCP client: one long-lived process per server
    found = []
    for server in mcp_client.servers():
        try:
            _mcp_tools[server] = {t["name"]: t["description"] for t in mcp_client.list_tools(server)}
            found.append(f"{server} ({len(_mcp_tools[server])} tools)")
        except Exception as error:  # e.g. its venv isn't set up: the router uses its own text for that server
            found.append(f"{server} unavailable ({error})")
    return ", ".join(found)


def mcp_description(server, tool):  # the live description of an MCP tool; None = router.TOOLS text (never waits)
    return _mcp_tools.get(server, {}).get(tool)


class JarvisSkill(Skill):  # one input for everything: the model (or router.py's rules) picks the skill, then it runs as usual
    name, title = "jarvis", "Ask Jarvis"
    description = ("Ask anything: questions go to your documents, YouTube requests (\"find shorts about cooking\", "
                   "@channel, a YouTube link) go to the youtube-shorts MCP server, \"make a meme about ...\" goes to the memes "
                   "MCP server, \"make a song about ...\" goes to the music MCP server, \"system check\" checks the "
                   "systems. The answer model picks the tool (or the fixed rules: Route with). Start with /docs, /shorts, "
                   "/meme or /song to choose yourself.")
    needs_question = True

    @property
    def options(self):  # the documents options plus the Shorts ones; each target reads only its own keys
        routing = [{"key": "router", "label": "Route with", "type": "select", "choices": ["model", "rules"], "default": ROUTER}]
        shorts = [dict(o, label="Shorts: " + o["label"].lower()) for o in SKILLS["shorts"].options if o["key"] != "mode"]
        memes = [dict(o, label="Meme: " + o["label"].lower()) for o in SKILLS["meme"].options]
        songs = [dict(o, label="Song: " + o["label"].lower()) for o in SKILLS["song"].options]
        return routing + SKILLS["rag"].options + shorts + memes + songs

    def validate(self, request):
        clean = super().validate(request)
        question = str(request.get("question", "")).strip()
        if not question:
            raise ValueError("Ask a question or make a request first")
        if len(question) > MAX_QUESTION:
            raise ValueError(f"Request is longer than {MAX_QUESTION} characters")
        history = clean_history(request.get("history"))
        target = router.route(question) if clean["router"] == "rules" else router.sure_route(question)
        if target is None:  # the model decides when the run starts ("Understand request"); then plan() as below
            return {**clean, "question": question, "history": history,
                    "route": {"skill": "pending", "title": "Understand request", "reason": "the model picks the tool"}}
        return self.plan(clean, question, history, target)

    def pick(self, request):  # ask the model which tool; the rules decide if it fails. -> (Route, problem or None)
        provider = request["provider"]
        model = request["model"] or rag.DEFAULT_MODELS[provider]
        try:
            choice = ask_json(router.tool_prompt(request["question"], request["history"], mcp_description),
                              router.TOOL_SCHEMA, provider, model, timeout=ROUTE_TIMEOUT, strict=False)
            return router.from_tool(choice.get("tool"), choice.get("input"), request["question"], f"{provider} · {model}"), None
        except Exception as error:  # Ollama off, no key, a bad reply: the patterns still work offline
            return router.route(request["question"]), f"model router failed ({type(error).__name__}: {error}), used the rules"

    def plan(self, clean, question, history, target):  # a Route -> the validated request of the skill(s) it runs
        mentioned = (vault_mentions(target.question) if target.skill == "shorts" and target.options.get("mode") == "search"
                     and not target.reason.startswith("/") else [])  # /shorts always means YouTube only
        if mentioned:  # "find shorts about Idan": Idan is in the notes -> what do the notes say about him, then YouTube
            words = " ".join(mentioned)
            asked = notes_question_about(target.question)
            target = router.Route("combo", asked, {**target.options, "fallback": target.question},
                                  f"“{words}” is in your notes → answer from the notes, then search Shorts about the answer")
        if target.skill == "combo":  # notes first, then YouTube: validate both halves now
            shorts = SKILLS["shorts"].validate({**clean, **target.options, "question": "(from the answer)"})
            follow_up = bool(target.options.get("follow_up"))
            if follow_up and not history:
                raise ValueError('About what? Ask a question first, or say what to look for ("find shorts about pizza")')
            inner = None if follow_up else SKILLS["rag"].validate({**clean, "question": target.question, "history": history})
            return {**clean, "question": question, "history": history,
                    "route": {"skill": "combo", "reason": target.reason, "title": "Notes → YouTube Shorts",
                              "request": inner, "shorts": shorts, "follow_up": follow_up,
                              "fallback": target.options.get("fallback")}}
        if target.skill == "shorts" and not target.question:
            raise ValueError("What should I look for on YouTube? e.g. \"find shorts about cooking\" or \"@MrBeast\"")
        skill = SKILLS[target.skill]
        inner = skill.validate({**clean, **target.options, "question": target.question, "history": history})  # rejects bad input up front
        return {**clean, "question": question, "history": history,
                "route": {"skill": target.skill, "reason": target.reason, "title": skill.title, "request": inner}}

    def stages(self, request):
        target = request["route"]
        if target["skill"] == "pending":  # the rest is known once the model has picked (a "stages" event)
            return [{"id": "route", "title": "Understand request"}]
        if target["skill"] == "combo":
            notes = SKILLS["rag"].stages(target["request"]) if target["request"] else []
            return [{"id": "route", "title": "Understand request"}, *notes,
                    {"id": "query", "title": "Answer → Shorts search"}, *SKILLS["shorts"].stages(target["shorts"])]
        return [{"id": "route", "title": "Understand request"}, *SKILLS[target["skill"]].stages(target["request"])]

    def run(self, request, run):
        run.emit("stage_start", stage="route", title="Understand request")
        started, outcome = time.time(), "ok"
        if request["route"]["skill"] == "pending":
            target, problem = self.pick(request)
            if problem:
                run.emit("log", stage="route", line=problem)
                outcome = "warning"
            run.check_cancelled()
            request.update(self.plan(request, request["question"], request["history"], target))  # may reject, like validate
            run.emit("stages", stages=self.stages(request))  # the monitor now shows the chosen pipeline
        target = request["route"]
        run.emit("log", stage="route", line=f"→ {target['title']}: {target['reason']}")
        run.emit("route", skill=target["skill"], title=target["title"], reason=target["reason"])
        run.emit("stage_end", stage="route", status=outcome, seconds=round(time.time() - started, 2))
        if target["skill"] != "combo":
            return SKILLS[target["skill"]].run(target["request"], run)
        if target.get("follow_up"):  # "find shorts about it": the previous answer in this conversation
            last = request["history"][-1]
            asked, claims = f"{last['question']} — {request['question']}", [last["answer"]]
        else:
            SKILLS["rag"].run(target["request"], run)  # 1. answer from the notes (shown and cited as usual)
            asked = request["question"]
            claims = [c["text"] for c in json.loads(rag.ANSWER.read_text(encoding="utf-8"))["answer"]["claims"]]
        run.emit("stage_start", stage="query", title="Answer → Shorts search")  # 2. what to look for, from that answer
        started = time.time()
        provider = request["provider"]
        model = request["model"] or rag.DEFAULT_MODELS[provider]
        try:
            if not claims and target.get("fallback"):  # the notes say nothing about it: search the topic as asked
                raise LookupError(target["fallback"])
            if not claims:  # the notes didn't answer: nothing to search for
                raise LookupError("")
            query = search_query(asked, claims, provider, model)
            run.emit("log", stage="query", line=f"{provider} · {model} → “{query}”")
        except LookupError as nothing:
            query = nothing.args[0] if nothing.args else ""
            if query:
                run.emit("log", stage="query", line=f"the notes say nothing about it: searching “{query}” as asked")
        except Exception as error:  # the search still runs, on the answer's own words
            query = fallback_query(claims)
            run.emit("log", stage="query", line=f"query model failed ({type(error).__name__}), using the answer: “{query}”")
        run.check_cancelled()
        run.emit("stage_end", stage="query", status="ok" if query else "warning", seconds=round(time.time() - started, 2))
        if not query:
            run.emit("message", text="The notes don't name anything to look up on YouTube, so I didn't search.")
            return
        SKILLS["shorts"].run({**target["shorts"], "question": query}, run)  # 3. the MCP tool


class JudgeSkill(Skill):  # LLM as a judge: grade a finished run with the model that answered it (judge.py)
    name, title = "judge", "Judge a response"
    description = "Grade a finished response: backed by its sources, right citations, right tool, complete, language."
    needs_question = False
    listed = False  # started from a result card's Judge button, with that run's id
    JUDGEABLE = {"jarvis", "rag", "shorts", "meme"}
    TIMEOUT = 180

    def validate(self, request):
        if not isinstance(request, dict):
            raise ValueError("Request must be an object")
        run_id = str(request.get("run_id") or "")
        if not re.fullmatch(r"[0-9A-Za-z-]{1,64}", run_id):
            raise ValueError("Which response? Send its run_id")
        target = self.manager.get(run_id) if self.manager else None
        if not target:
            raise ValueError(f"Run {run_id} not found")
        if target.status == "running":
            raise ValueError("The run is still going: judge it when it ends")
        data = target.to_json()
        case = judge.case_from_run(data["events"], data["request"]) if data["summary"]["skill"] in self.JUDGEABLE else None
        if target.status != "done" or not case:
            raise ValueError("Nothing to judge here: only a finished answer, Shorts result or meme can be judged")
        provider, model = self.judge_model(case, data["request"])
        return {"run_id": run_id, "question": "Judge: " + (case["question"] or run_id)[:200], "case": case,
                "criteria": judge.applicable(case), "provider": provider, "model": model,
                "target_skill": data["summary"]["skill"]}

    @staticmethod
    def judge_model(case, request):  # the same model that wrote the response
        if case["answer"] and case["answer"]["provider"] in rag.PROVIDERS and case["answer"]["model"]:
            return case["answer"]["provider"], case["answer"]["model"]  # a documents answer: its own model
        inner = (request.get("route") or {}).get("request") or {}
        if case["meme"]:  # the captions model ("Meme: captions by"), not the documents' "Answer with"
            provider = inner.get("captions_by") or request.get("captions_by")
            if provider in rag.PROVIDERS:
                return provider, rag.DEFAULT_MODELS[provider]
        provider = request.get("provider") or inner.get("provider")  # Shorts: the model the run was asked to use
        if provider in rag.PROVIDERS:
            return provider, request.get("model") or inner.get("model") or rag.DEFAULT_MODELS[provider]
        return "ollama", rag.DEFAULT_MODELS["ollama"]

    @staticmethod
    def failure(error, provider):  # a short reason the judge model failed, that says what to fix
        code = getattr(error, "code", None)  # urllib's HTTPError
        if code in (401, 403):
            return f"{provider} rejected the API key (HTTP {code}). Put a valid key in your .env / atlas-credentials.env"
        if code == 404:
            return f"{provider} doesn't know this model (HTTP 404)"
        if code == 429:
            return f"{provider} rate limit or quota reached (HTTP 429); try again later"
        if isinstance(error, ValueError):  # no key, or a reply that isn't JSON
            return str(error)
        return f"{type(error).__name__}: {error} (is {provider} reachable?)"

    def stages(self, request):
        return [{"id": "collect", "title": "Read the response"},
                {"id": "judge", "title": f"Judge · {request['provider']} · {request['model']}"},
                {"id": "save", "title": "Save to MongoDB"}]

    COLLECTION = "judgements"  # in the same Atlas database as the chunk vectors (atlas-credentials.env)

    @classmethod
    def save(cls, document):  # one judgement -> MongoDB Atlas; returns "db.collection". Raises with a short, safe message
        if str(rag.EX1) not in sys.path:
            sys.path.append(str(rag.EX1))
        import atlas_upload  # exercise 1's helpers: credentials lookup + connect (needs pymongo)
        settings, problem = atlas_upload.atlas_settings(rag.EX1)
        if problem:
            raise LookupError(problem)
        try:
            client = atlas_upload.connect(settings["uri"])
            try:
                client[settings["db"]][cls.COLLECTION].replace_one({"_id": document["_id"]}, document, upsert=True)
            finally:
                client.close()
        except Exception as error:  # never the URI or the cluster host
            raise RuntimeError(status.atlas_error_detail(error, settings, atlas_upload.safe_error)) from None
        return f"{settings['db']}.{cls.COLLECTION}"

    def run(self, request, run):
        case, ids = request["case"], request["criteria"]
        run.emit("stage_start", stage="collect", title="Read the response")
        answer = case["answer"]
        what = (f"{len(answer['claims'])} claims, {len(answer['sources'])} sources" if answer else
                "YouTube Shorts" if case["posts"] else "a meme")
        run.emit("log", stage="collect", line=f"run {request['run_id']}: {what}; {len(case['tool_calls'])} tool call(s)")
        run.emit("log", stage="collect", line="criteria: " + ", ".join(judge.CRITERIA[i][0] for i in ids))
        run.emit("stage_end", stage="collect", status="ok", seconds=0)
        run.emit("stage_start", stage="judge", title=f"Judge · {request['provider']} · {request['model']}")
        started = time.time()
        try:
            reply = ask_json(judge.prompt(case, ids), judge.schema(case, ids), request["provider"], request["model"],
                             timeout=self.TIMEOUT, num_predict=1500, max_tokens=2000)
        except Exception as error:  # no key, model down, timeout, a reply that isn't JSON
            run.check_cancelled()
            run.emit("stage_end", stage="judge", status="error", seconds=round(time.time() - started, 2))
            raise RuntimeError(f"Judge failed — {self.failure(error, request['provider'])}") from None
        run.check_cancelled()
        seconds = round(time.time() - started, 2)
        result = judge.score(case, ids, reply)
        missing = sum(r["score"] is None for r in result["criteria"] if r["id"] in ids)
        run.emit("log", stage="judge", line=f"overall {result['overall']}/5"
                 + (f", {missing} criteria without a verdict" if missing else ""))
        run.emit("stage_end", stage="judge", status="warning" if missing or result["overall"] is None else "ok",
                 seconds=seconds)
        run.emit("judgement", target_run=request["run_id"], question=case["question"], provider=request["provider"],
                 model=request["model"], same_model=True, seconds=seconds, **result)
        run.emit("stage_start", stage="save", title="Save to MongoDB")  # after the scorecard: a slow database never hides it
        started = time.time()
        document = {"_id": run.id, "judged_at": datetime.datetime.now(datetime.timezone.utc), "target_run": request["run_id"],
                    "target_skill": request.get("target_skill"), "route": (case["route"] or {}).get("skill"),
                    "question": case["question"], "answered_question": (answer or {}).get("question"),
                    "provider": request["provider"], "model": request["model"], "same_model": True,
                    "criteria_asked": ids, "seconds": seconds, **result}
        try:
            where = self.save(document)
            run.emit("log", stage="save", line=f"saved to {where} (_id {run.id})")
            run.emit("stage_end", stage="save", status="ok", seconds=round(time.time() - started, 2))
            run.emit("judgement_saved", target_run=request["run_id"], saved_to=where)
        except Exception as error:  # the scorecard still counts; only the database copy is missing
            detail = str(error) or type(error).__name__
            run.emit("log", stage="save", line=f"not saved to MongoDB: {detail}")
            run.emit("stage_end", stage="save", status="warning", seconds=round(time.time() - started, 2))
            run.emit("judgement_saved", target_run=request["run_id"], saved_to=None, error=detail)


SKILLS = {skill.name: skill for skill in (JarvisSkill(), RagSkill(), ShortsSkill(), MemeSkill(), SongSkill(), VaultSyncSkill(), StatusSkill(),
                                          JudgeSkill())}


def describe():  # what the console needs to draw the skill picker and its options
    return [{"name": s.name, "title": s.title, "description": s.description, "options": s.options,
             "needs_question": getattr(s, "needs_question", True)} for s in SKILLS.values() if s.listed]
