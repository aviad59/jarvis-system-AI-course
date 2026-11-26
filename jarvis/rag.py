"""End-to-end RAG: index (exercise 1) -> retrieve (exercise 2) -> answer with Ollama, Claude or Gemini (exercise 3).

Each stage runs the exercise's own script in its own folder, so the exercises stay
independent and every intermediate file is kept where you already know to look:

  1. exercise-1-indexing/build/index.json   + MongoDB Atlas  (chunks with embeddings; Atlas is used only with --source mongodb)
  2. exercise-2-retrieval/build/search-results.json         (ranked evidence)
  3. exercise-3-generation/artifacts/answer.json + .html     (cited answer + viewer)

Usage (from this folder, with Ollama running for the embeddings):
  python rag.py --question "מתי מסלימים תקלה בהתחברות?"
  python rag.py --question "HTTP 401" --hybrid --skip-index
  python rag.py --question "HTTP 401" --source mongodb    # store and search the vectors in MongoDB Atlas

The same steps are used by the Jarvis console (jarvis/server.py) to run and monitor the pipeline.
"""
import argparse  # command-line options
import json  # read which documents the saved index holds
import getpass  # type the API key without echoing it
import os  # pass the API key to the generation stage through the environment
import subprocess  # run each exercise's script in its own folder
import sys  # the current Python interpreter and exit codes
from pathlib import Path  # folder paths

ROOT = Path(__file__).resolve().parent
EX1, EX2, EX3 = ROOT / "exercise-1-indexing", ROOT / "exercise-2-retrieval", ROOT / "exercise-3-generation"
INDEX_FILE = EX1 / "build" / "index.json"  # the local copy of the index, written by every embed run
SEARCH_RESULTS = EX2 / "build" / "search-results.json"
ANSWER = EX3 / "artifacts" / "answer.json"
HISTORY = EX3 / "artifacts" / "history.json"  # the conversation so far, for the answer model (Jarvis)
KEY_FILES = tuple(folder / name for folder in (ROOT, ROOT / "jarvis", EX3, EX1)  # where ANTHROPIC_API_KEY=... / GEMINI_API_KEY=... may live
                  for name in (".env", "atlas-credentials.env"))
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "gemini": "gemini-3.8-flash", "ollama": "gemma3:4b"}
PROVIDERS = ("ollama", "anthropic", "gemini")  # who can write the answer; the embeddings always come from Ollama
API_KEYS = {"anthropic": ("ANTHROPIC_API_KEY",), "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY")}  # cloud providers only
# Session 5: the Obsidian vault with the team notes. Override with JARVIS_VAULT=<folder>.
VAULT = Path(os.environ.get("JARVIS_VAULT") or Path.home() / "Documents" / "Obsidian Vault")
DOCUMENT_SOURCES = ("vault", "course", "both")  # vault notes, the session-4 incident documents, or both
GENERATION_TIMEOUT = {"anthropic": 300, "gemini": 300, "ollama": 900}  # seconds; a 4B model on a CPU can need several minutes


def find_key(*names):  # (key, where it came from) or (None, None) for the first of these variables; never printed
    for name in names:
        if os.environ.get(name):
            return os.environ[name], "environment"
    for name in names:
        for path in KEY_FILES:
            if path.exists():
                for line in path.read_text(encoding="utf-8-sig").splitlines():
                    key, _, value = line.strip().removeprefix("export ").partition("=")
                    if key.strip().upper() == name and value.strip():
                        return value.strip().strip("'\""), str(path.relative_to(ROOT))
    return None, None


def find_anthropic_key():
    return find_key(*API_KEYS["anthropic"])


def find_gemini_key():  # GEMINI_API_KEY, or GOOGLE_API_KEY as Google's own SDKs also accept
    return find_key(*API_KEYS["gemini"])


def provider_env(provider):  # {"<KEY VARIABLE>": key} for a cloud provider, {} for Ollama; ValueError if the key is missing
    if provider not in API_KEYS:
        return {}
    key, _ = find_key(*API_KEYS[provider])
    if not key:
        raise ValueError(f"{API_KEYS[provider][0]} is not set. Add {API_KEYS[provider][0]}=... to a .env file next to "
                         "rag.py, or choose the ollama provider")
    return {API_KEYS[provider][0]: key}  # the generation stage reads the first name only


_index_cache = {"key": None, "corpus": None, "vault": {}}


def _index_info():  # (corpus, {vault source: sha256}) of the saved index, re-read only when the file changes
    try:
        stat = INDEX_FILE.stat()
    except OSError:
        return None, {}
    key = (stat.st_mtime_ns, stat.st_size)
    if _index_cache["key"] != key:
        try:
            data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            corpus = data["manifest"].get("corpus", "course")
            vault = {r["metadata"]["source"]: r["document_sha256"] for r in data["records"]
                     if r["metadata"].get("source", "").startswith("vault/")}
        except (OSError, ValueError, KeyError):
            corpus, vault = None, {}
        _index_cache.update(key=key, corpus=corpus, vault=vault)
    return _index_cache["corpus"], _index_cache["vault"]


def index_corpus():  # which documents the saved index was built from ("course" for indexes built before session 5)
    return _index_info()[0]


def _vault_support():  # exercise 1's vault reader, loaded by path (exercise 2 has its own index_support module)
    import importlib.util
    if "ex1_index_support" not in sys.modules:
        spec = importlib.util.spec_from_file_location("ex1_index_support", EX1 / "index_support.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["ex1_index_support"] = module
    return sys.modules["ex1_index_support"]


def index_is_stale(documents="vault"):  # True when the saved index doesn't match these documents (new, edited or deleted notes)
    corpus, indexed = _index_info()
    if corpus != documents:
        return True
    if documents == "course":
        return False
    try:
        return _vault_support().vault_signature(VAULT) != indexed
    except ValueError:  # vault folder missing: let the indexing step report it
        return True


def index_steps(documents="vault", source="local", structured=False, embedding_model="embeddinggemma"):
    """Exercise 1: chunk and embed (reusing unchanged vectors), and sync to MongoDB unless source is local."""
    if source not in ("mongodb", "local"):
        raise ValueError("source must be 'mongodb' or 'local'")
    if documents not in DOCUMENT_SOURCES:
        raise ValueError(f"documents must be one of {DOCUMENT_SOURCES}")
    local = source == "local"
    vault_args = [] if documents == "course" else ["--vault", str(VAULT), *(["--no-course-docs"] if documents == "vault" else [])]
    reuse = ["--reuse", "build/index.json"] if INDEX_FILE.exists() else []
    return [{"id": "chunk", "title": "Chunking", "folder": EX1,
             "args": ["build_index.py", "prepare", *vault_args, *(["--structured"] if structured else [])]},
            {"id": "embed", "title": "Embeddings → local file" if local else "Embeddings → MongoDB", "folder": EX1,
             "args": ["build_index.py", "embed", "--provider", "ollama", "--model", embedding_model, *reuse,
                      *(["--no-atlas"] if local else [])]}]


def build_steps(question, skip_index=False, structured=False, embedding_model="embeddinggemma", hybrid=False,
                top_k=3, exclude_superseded=False, provider="ollama", model=None, source="local", documents="vault",
                history_file=None, index_busy=False):
    """Return the pipeline as a list of steps: {'id', 'title', 'folder', 'args'} (args exclude the Python executable).

    source="mongodb" stores and reads the vectors in MongoDB Atlas; source="local" uses only INDEX_FILE.
    documents="vault" indexes the Obsidian vault (VAULT), "course" the incident documents, "both" all of them.
    skip_index is ignored when the saved index is out of date (other documents, or the vault changed), unless
    index_busy: a sync is rebuilding the index right now, so the last finished one is searched as it is.
    """
    steps = index_steps(documents, source, structured, embedding_model)  # also validates source and documents
    if skip_index and (index_busy or not index_is_stale(documents)):
        steps = []
    local = source == "local"
    model = model or DEFAULT_MODELS[provider]
    steps.append({"id": "retrieve", "title": ("Hybrid retrieval" if hybrid else "Retrieval") + (" (local file)" if local else ""),
                  "folder": EX2,  # vectors from MongoDB, or from INDEX_FILE when local
                  "args": ["hybrid_search.py" if hybrid else "search.py", "--question", question, "--top-k", str(top_k),
                           "--output", str(SEARCH_RESULTS), *(["--exclude-superseded"] if exclude_superseded else []),
                           *(["--source", "file", "--index", str(INDEX_FILE)] if local else [])]})
    steps.append({"id": "answer", "title": f"Answer ({model})", "folder": EX3,
                  "args": ["run.py", "--input", str(SEARCH_RESULTS), "--provider", provider, "--model", model,
                           "--output", str(ANSWER), "--require-citations", "--timeout", str(GENERATION_TIMEOUT[provider]),
                           *(["--history", str(history_file)] if history_file else [])]})
    return steps


def main():
    parser = argparse.ArgumentParser(description="Run exercises 1-3 as one RAG pipeline")
    parser.add_argument("--question", required=True)
    parser.add_argument("--skip-index", action="store_true", help="Use the existing index (skip exercise 1)")
    parser.add_argument("--source", choices=("local", "mongodb"), default="local",
                        help="Where the vectors live: MongoDB Atlas, or only the local build/index.json")
    parser.add_argument("--documents", choices=DOCUMENT_SOURCES, default="vault",
                        help=f"What to index: the Obsidian vault ({VAULT}), the course documents, or both")
    parser.add_argument("--structured", action="store_true", help="Exercise 1 bonus: structure-aware chunking")
    parser.add_argument("--embedding-model", default="embeddinggemma", help="Ollama embedding model")
    parser.add_argument("--hybrid", action="store_true", help="Exercise 2 bonus: semantic + BM25 fused with RRF")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--exclude-superseded", action="store_true", help="Drop documents replaced by a newer version")
    parser.add_argument("--provider", choices=PROVIDERS, default="ollama", help="Generation provider")
    parser.add_argument("--model", help="Generation model (default: gemma3:4b; claude-opus-5-5 for anthropic; "
                                        "gemini-3.8-flash for gemini)")
    parser.add_argument("--ask-key", action="store_true", help="Type the Anthropic or Gemini API key privately")
    args = parser.parse_args()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if args.provider in API_KEYS:  # checked up front, so we don't index and search only to fail at the last stage
        name = API_KEYS[args.provider][0]
        key, _ = find_key(*API_KEYS[args.provider])
        if not key and args.ask_key:
            key = getpass.getpass(f"{args.provider.capitalize()} API key (hidden): ")
        if not key:
            raise SystemExit(f"STOP: {name} is not set. Set it, add {name}=... to a .env file, or add --ask-key")
        env[name] = key
    steps = build_steps(args.question, args.skip_index, args.structured, args.embedding_model, args.hybrid,
                        args.top_k, args.exclude_superseded, args.provider, args.model, args.source, args.documents)
    for number, step in enumerate(steps, 1):
        print(f"\n=== {number}/{len(steps)} {step['title']} ===", flush=True)
        code = subprocess.call([sys.executable, *step["args"]], cwd=step["folder"], env=env)
        if code != 0:
            raise SystemExit(f"STOP: {step['title']} failed (exit code {code})")
    print(f"\nOpen the answer with its sources: {ANSWER.with_suffix('.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
