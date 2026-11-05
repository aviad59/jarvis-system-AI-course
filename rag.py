"""End-to-end RAG: index (exercise 1) -> retrieve (exercise 2) -> answer with Claude (exercise 3).

Each stage runs the exercise's own script in its own folder, so the exercises stay
independent and every intermediate file is kept where you already know to look:

  1. exercise-1-indexing/build/index.json   + MongoDB Atlas  (chunks with embeddings)
  2. exercise-2-retrieval/build/search-results.json         (ranked evidence)
  3. exercise-3-generation/artifacts/answer.json + .html     (cited answer + viewer)

Usage (from this folder, with Ollama running for the embeddings):
  python rag.py --question "מתי מסלימים תקלה בהתחברות?"
  python rag.py --question "HTTP 401" --hybrid --skip-index

The same steps are used by the Jarvis console (jarvis/server.py) to run and monitor the pipeline.
"""
import argparse  # command-line options
import getpass  # type the API key without echoing it
import os  # pass the API key to the generation stage through the environment
import subprocess  # run each exercise's script in its own folder
import sys  # the current Python interpreter and exit codes
from pathlib import Path  # folder paths

ROOT = Path(__file__).resolve().parent
EX1, EX2, EX3 = ROOT / "exercise-1-indexing", ROOT / "exercise-2-retrieval", ROOT / "exercise-3-generation"
SEARCH_RESULTS = EX2 / "build" / "search-results.json"
ANSWER = EX3 / "artifacts" / "answer.json"
KEY_FILES = (ROOT / ".env", EX3 / ".env")  # optional files holding ANTHROPIC_API_KEY=...
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "ollama": "gemma3:4b"}
GENERATION_TIMEOUT = {"anthropic": 300, "ollama": 900}  # seconds; a 4B model on a CPU can need several minutes


def find_anthropic_key():  # (key, where it came from) or (None, None); the key is never printed
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"], "environment"
    for path in KEY_FILES:
        if path.exists():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key, _, value = line.strip().removeprefix("export ").partition("=")
                if key.strip() == "ANTHROPIC_API_KEY" and value.strip():
                    return value.strip().strip("'\""), str(path.relative_to(ROOT))
    return None, None


def build_steps(question, skip_index=False, structured=False, embedding_model="embeddinggemma", hybrid=False,
                top_k=3, exclude_superseded=False, provider="anthropic", model=None):
    """Return the pipeline as a list of steps: {'id', 'title', 'folder', 'args'} (args exclude the Python executable)."""
    model = model or DEFAULT_MODELS[provider]
    steps = []
    if not skip_index:  # exercise 1: chunk, embed (reusing unchanged vectors) and sync to MongoDB
        steps.append({"id": "chunk", "title": "Chunking", "folder": EX1,
                      "args": ["build_index.py", "prepare", *(["--structured"] if structured else [])]})
        reuse = ["--reuse", "build/index.json"] if (EX1 / "build" / "index.json").exists() else []
        steps.append({"id": "embed", "title": "Embeddings → MongoDB", "folder": EX1,
                      "args": ["build_index.py", "embed", "--provider", "ollama", "--model", embedding_model, *reuse]})
    steps.append({"id": "retrieve", "title": "Hybrid retrieval" if hybrid else "Retrieval", "folder": EX2,  # vectors from MongoDB
                  "args": ["hybrid_search.py" if hybrid else "search.py", "--question", question, "--top-k", str(top_k),
                           "--output", str(SEARCH_RESULTS), *(["--exclude-superseded"] if exclude_superseded else [])]})
    steps.append({"id": "answer", "title": f"Answer ({model})", "folder": EX3,
                  "args": ["run.py", "--input", str(SEARCH_RESULTS), "--provider", provider, "--model", model,
                           "--output", str(ANSWER), "--require-citations", "--timeout", str(GENERATION_TIMEOUT[provider])]})
    return steps


def main():
    parser = argparse.ArgumentParser(description="Run exercises 1-3 as one RAG pipeline")
    parser.add_argument("--question", required=True)
    parser.add_argument("--skip-index", action="store_true", help="Use the index already in MongoDB (skip exercise 1)")
    parser.add_argument("--structured", action="store_true", help="Exercise 1 bonus: structure-aware chunking")
    parser.add_argument("--embedding-model", default="embeddinggemma", help="Ollama embedding model")
    parser.add_argument("--hybrid", action="store_true", help="Exercise 2 bonus: semantic + BM25 fused with RRF")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--exclude-superseded", action="store_true", help="Drop documents replaced by a newer version")
    parser.add_argument("--provider", choices=("anthropic", "ollama"), default="anthropic", help="Generation provider")
    parser.add_argument("--model", help="Generation model (default: claude-opus-5-5, or gemma3:4b for ollama)")
    parser.add_argument("--ask-key", action="store_true", help="Type the Anthropic API key privately")
    args = parser.parse_args()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if args.provider == "anthropic":  # checked up front, so we don't index and search only to fail at the last stage
        key, _ = find_anthropic_key()
        if not key and args.ask_key:
            key = getpass.getpass("Anthropic API key (hidden): ")
        if not key:
            raise SystemExit("STOP: ANTHROPIC_API_KEY is not set. Set it, put it in a .env file next to rag.py, or add --ask-key")
        env["ANTHROPIC_API_KEY"] = key
    steps = build_steps(args.question, args.skip_index, args.structured, args.embedding_model, args.hybrid,
                        args.top_k, args.exclude_superseded, args.provider, args.model)
    for number, step in enumerate(steps, 1):
        print(f"\n=== {number}/{len(steps)} {step['title']} ===", flush=True)
        code = subprocess.call([sys.executable, *step["args"]], cwd=step["folder"], env=env)
        if code != 0:
            raise SystemExit(f"STOP: {step['title']} failed (exit code {code})")
    print(f"\nOpen the answer with its sources: {ANSWER.with_suffix('.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
