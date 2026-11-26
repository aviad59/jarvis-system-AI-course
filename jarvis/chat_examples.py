"""End-to-end chat examples: self-query (answer model) -> filtered $vectorSearch (MongoDB Atlas) -> check the hits.

Needs: Ollama running (embeddings, and the self-query model for --provider ollama), the chat migrated into
MongoDB (exercise-1-indexing/ingest_chats.py) and atlas-credentials.env. Writes nothing to the database.

  python chat_examples.py
  python chat_examples.py --provider anthropic
  python chat_examples.py --question "מה עומר אמר על הקזינו?"
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import rag

EXAMPLES = (
    "מה עומר אמר על אמנון?",  # what did <sender> say about X
    "מה דיברו ב-22 בספטמבר?",  # what was discussed on <date>
    "what did Yarden say about the beer in September?",  # <sender> + topic + month
    "מה ירדן אמר על הבירה בשבוע שעבר?",  # last week has no messages: the filters must be relaxed
)


def run(question, provider, model, top_k):
    sys.path.insert(0, str(rag.ROOT / "jarvis"))
    from skills import ask_json
    found = rag.self_query(question, lambda prompt, schema: ask_json(prompt, schema, provider, model))
    print(f"\nQUESTION: {question}")
    print(f"  self-query: query={found['query']!r} senders={found['senders']} from={found['from']!r} to={found['to']!r}"
          + (f" dropped={found['dropped']}" if found["dropped"] else ""))
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder) / "results.json"
        args = [sys.executable, "search.py", "--question", question, "--query", found["query"], "--top-k", str(top_k),
                "--output", str(output), *(["--senders", ",".join(found["senders"])] if found["senders"] else []),
                *(["--from", found["from"]] if found["from"] else []), *(["--to", found["to"]] if found["to"] else [])]
        done = subprocess.run(args, cwd=rag.EX2, capture_output=True, text=True, encoding="utf-8",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if done.returncode:
            print("  FAIL: search: " + (done.stderr.strip() or done.stdout.strip()))
            return False
        data = json.loads(output.read_text(encoding="utf-8"))
    retrieval = data["retrieval"]
    print(f"  method={retrieval['method']} relaxed={retrieval.get('relaxed')} applied={retrieval.get('applied_filters')}")
    ok = bool(data["results"])
    applied = retrieval.get("applied_filters") or {}
    for r in data["results"]:
        first = (r.get("messages") or [{}])[0]
        print(f"  #{r['rank']} {r['score']:.3f} {r['chunk_id']} {r.get('start_ts')}..{r.get('end_ts')} "
              f"{r.get('participants')}  “{first.get('text', r['text'])[:60]}”")
        if applied.get("senders") and not set(applied["senders"]) & set(r.get("participants") or []):
            print("    FAIL: none of the senders wrote in this chunk")
            ok = False
        start, end = (datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None
                      for v in (applied.get("from"), applied.get("to")))
        if (start or end) and r.get("start_ts"):
            s = datetime.fromisoformat(r["start_ts"].replace("Z", "+00:00"))
            e = datetime.fromisoformat(r["end_ts"].replace("Z", "+00:00"))
            if (end and s > end) or (start and e < start):
                print("    FAIL: chunk is outside the time range")
                ok = False
    if found["filtered"] and not retrieval["method"].endswith("filtered_exact"):
        print("  FAIL: filters were extracted but the search was not filtered")
        ok = False
    print("  PASS" if ok else "  FAIL")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--question", action="append", help="Run this question instead of the examples (repeatable)")
    parser.add_argument("--provider", choices=rag.PROVIDERS, default="ollama")
    parser.add_argument("--model")
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()
    for name, key in rag.provider_env(args.provider).items():
        os.environ[name] = key
    model = args.model or rag.DEFAULT_MODELS[args.provider]
    print(f"today: {datetime.now(timezone.utc):%Y-%m-%d} · self-query model: {args.provider}/{model}")
    results = [run(q, args.provider, model, args.top_k) for q in (args.question or EXAMPLES)]
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
