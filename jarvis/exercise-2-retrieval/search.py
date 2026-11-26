"""Run Retrieval over the index built in exercise 1.

By default the chunk vectors and the manifest are loaded from MongoDB Atlas (where
exercise 1's embed stage saved them), and ranked locally with your cosine_similarity
and select_top_k. Use --source file to read build/index.json instead, or
--backend atlas to let Atlas Vector Search (HNSW) do the ranking on the server.

The provider, model, dimensions and input format come from the manifest, so the
question is embedded in exactly the same vector space as the documents.
"""
import argparse  # command-line options
import getpass  # type secrets without echoing them
import json  # read the local index and write the results file
import math  # validate scores returned by cosine_similarity
import os  # read connection strings from the environment
import sys  # exit codes, error output, and access to exercise 1's Atlas helpers
from pathlib import Path  # file paths
import chat_filters  # sender / time filters for chat chunks
from retrieval import cosine_similarity, select_top_k  # the two ranking functions students implement
from index_support import (resolve_model, embed_texts,  # embedding helpers shared with exercise 1
                           fixture_vectors, validate_vectors)

EXERCISE1 = Path(__file__).resolve().parent.parent / "exercise-1-indexing"  # holds atlas_upload.py and atlas-credentials.env


def validate_index(manifest, records, check_count=True):  # same checks whether the index came from a file or from the DB
    if manifest["embedding_mode"] not in ("ollama", "offline_fixture"):  # only providers we can embed a query with
        raise ValueError("Unknown embedding provider in index manifest")
    notes = sum(1 for record in records if record.get("kind") != "chat")  # chunk_count counts note chunks; chats are synced apart
    if check_count and (notes != manifest["chunk_count"] or not records):  # a partial upload or empty collection
        raise ValueError(f"Index has {notes} note records but its manifest says chunk_count={manifest['chunk_count']}")
    if len({record["chunk_id"] for record in records}) != len(records):  # every chunk must be uniquely identifiable
        raise ValueError("Index contains duplicate chunk IDs")
    for record in records:  # every result must be traceable to a source
        if not record.get("text", "").strip() or not record["metadata"].get("source"):
            raise ValueError("Every index record needs text and source metadata")
    if validate_vectors([record["embedding"] for record in records]) != manifest["dimensions"]:  # vectors match the manifest
        raise ValueError("Index vector dimensions do not match its manifest")
    return manifest, records


def read_index_file(path):  # load build/index.json from exercise 1
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("stage") != "index":  # must be the output of the embed stage
        raise ValueError("Use build/index.json from the embed stage of exercise 1")
    return validate_index(data["manifest"], data["records"])


def atlas_connection(args):  # open a MongoDB connection using exercise 1's credentials file
    sys.path.append(str(EXERCISE1))  # appended, so this folder's own index_support.py still wins
    import atlas_upload  # credential lookup + connect helpers; requires pymongo
    if args.ask_uri and not os.environ.get("MONGODB_URI"):  # optional: type the URI instead of using a file
        os.environ["MONGODB_URI"] = getpass.getpass("Atlas connection string (hidden): ")  # kept only in process memory
    settings, problem = atlas_upload.atlas_settings(EXERCISE1, args.atlas_env)  # env vars override the file
    if problem:  # no connection details anywhere
        raise ValueError(f"{problem}. Add atlas-credentials.env in exercise-1-indexing, or use --source file")
    for key in ("db", "collection", "vector_index"):  # command-line values override the file
        if getattr(args, key):
            settings[key] = getattr(args, key)
    try:
        client = atlas_upload.connect(settings["uri"])  # fails fast on a bad URI or blocked IP
    except Exception as error:  # never show the URI (it contains the password)
        raise ValueError("Atlas connection failed: " + atlas_upload.safe_error(error, settings)) from None
    print(f"SOURCE: MongoDB db={settings['db']} collection={settings['collection']} (credentials from {settings['source']})")
    return client, settings


def load_manifest(db, collection):  # the manifest tells us how the stored vectors were computed
    manifest = db["index_manifests"].find_one({"_id": collection}, {"_id": 0})
    if not manifest:  # nothing was uploaded yet
        raise ValueError("No manifest in Atlas. Run exercise 1's embed stage first")
    return manifest


CHAT_FIELDS = ("kind", "messages", "participants", "start_ts", "end_ts", "chat_file")  # chat chunks only
OUTPUT_FIELDS = {"_id": 0, "chunk_id": 1, "text": 1, "metadata": 1, **{field: 1 for field in CHAT_FIELDS}}


def read_index_db(db, collection, query=None):  # load chunks with their stored embedding from MongoDB
    """query: an optional MongoDB filter (sender/time filters); the count check then no longer applies."""
    manifest = load_manifest(db, collection)
    fields = {**OUTPUT_FIELDS, "embedding": 1}  # only what ranking and output need
    records = list(db[collection].find(query or {}, fields).sort("chunk_id", 1))  # stable order for reproducible runs
    if query is not None and not records:
        return manifest, []
    return validate_index(manifest, records, check_count=query is None)


def _stamp(value):  # a stored date (pymongo: naive UTC) or an ISO string -> ISO string
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def chat_fields(record):  # who said what and when, for a chat chunk (empty for a note); JSON-ready
    chat = record.get("chat") or record  # build/index.json keeps them under "chat", MongoDB at the top level
    if record.get("kind") != "chat" or not chat.get("messages"):
        return {}
    return {"messages": [{**m, "ts": _stamp(m["ts"])} for m in chat["messages"]],
            "participants": chat.get("participants", []),
            "start_ts": _stamp(chat.get("start_ts")), "end_ts": _stamp(chat.get("end_ts"))}


def query_embedding(question, manifest, args):  # embed the question in the same space as the documents
    text = question
    if manifest["document_format"] == "embeddinggemma":  # EmbeddingGemma uses a different prefix for queries than documents
        text = "task: search result | query: " + question
    elif manifest["document_format"] != "none":
        raise ValueError("Unsupported document_format in the index")
    mode = manifest["embedding_mode"]
    if mode == "offline_fixture":
        if not args.allow_fixture:  # fixture vectors are not semantic; require an explicit opt-in
            raise ValueError("Fixture index requires --allow-fixture; it cannot test semantic retrieval")
        vectors = fixture_vectors([text])
    else:  # ollama
        model, digest = resolve_model(args.base_url, manifest["model"])
        if digest != manifest["model_digest"]:  # different weights would produce an incompatible vector space
            raise ValueError("Installed model changed. Rebuild exercise 1 index before searching")
        vectors = embed_texts(args.base_url, model, [text])
    if validate_vectors(vectors) != manifest["dimensions"]:
        raise ValueError("Query dimensions differ from index; rebuild using a compatible model")
    return vectors[0]


def rank_locally(args, question, manifest, records):  # score every candidate with the student's functions
    query = query_embedding(question, manifest, args)
    candidates = [record for record in records  # optional version filter
                  if not (args.exclude_superseded and record["metadata"]["status"] == "superseded")]
    scored = []
    for record in candidates:
        score = cosine_similarity(query, record["embedding"])
        if not isinstance(score, (float, int)) or not math.isfinite(score) or not -1.000001 <= score <= 1.000001:
            raise ValueError("Cosine Similarity must return a finite score between -1 and 1")
        scored.append({"chunk_id": record["chunk_id"], "text": record["text"],
                       "metadata": record["metadata"], "score": float(score), **chat_fields(record)})
    selected = select_top_k(scored, args.top_k)
    # Verify the selection: correct order, and no invented chunks or scores.
    expected_ids = [record["chunk_id"] for record in sorted(scored, key=lambda item: (-item["score"], item["chunk_id"]))[:args.top_k]]
    if [record["chunk_id"] for record in selected] != expected_ids:
        raise ValueError("Top-k selection must preserve the correct order and candidate IDs")
    originals = {record["chunk_id"]: record for record in scored}  # export the original record, not whatever top-k returned
    results = [{"rank": rank, **originals[record["chunk_id"]]} for rank, record in enumerate(selected, 1)]
    return results, len(candidates)


def atlas_vector_search(args, question, db, settings, chat_filter=None, exact=None, query=None):
    """Let Atlas rank on the server (HNSW, cosine). chat_filter (sender/time) is applied inside the same
    $vectorSearch, before the top-k is taken, not after it. exact=True: exhaustive ENN."""
    manifest = load_manifest(db, settings["collection"])
    query = query or query_embedding(question, manifest, args)
    stage = {"index": settings["vector_index"], "path": "embedding", "queryVector": query,
             "numCandidates": max(100, args.top_k * 20), "limit": args.top_k}  # ANN: explore more candidates than needed
    if args.exact if exact is None else exact:  # ENN: exhaustive, exact scan instead of HNSW
        stage.pop("numCandidates")
        stage["exact"] = True
    # filtering happens inside the search; the index declares these paths as filter fields
    superseded = {"metadata.status": {"$ne": "superseded"}} if args.exclude_superseded else None
    search_filter = chat_filters.combine(chat_filter, superseded)
    if search_filter:
        stage["filter"] = search_filter
    pipeline = [{"$vectorSearch": stage},
                {"$project": {**OUTPUT_FIELDS, "score": {"$meta": "vectorSearchScore"}}}]
    hits = list(db[settings["collection"]].aggregate(pipeline))
    results = []
    for rank, hit in enumerate(hits, 1):  # Atlas reports cosine as (1 + cos) / 2 in [0, 1]
        score = round(hit["score"] * 2 - 1, 6)  # convert back to plain cosine similarity in [-1, 1]
        results.append({"rank": rank, "chunk_id": hit["chunk_id"], "text": hit["text"], "metadata": hit["metadata"],
                        "score": score, **chat_fields(hit)})
    return manifest, results, db[settings["collection"]].estimated_document_count()


def filtered_search(args, question, db, settings, filters):
    """Sender/time filters: one filtered $vectorSearch (exact, so the ranking equals plain cosine). If no chunk
    matches, retry with relaxed filters (time first, then senders) and say what was relaxed."""
    manifest = load_manifest(db, settings["collection"])
    query = query_embedding(question, manifest, args)
    for relaxed, attempt in chat_filters.relaxations(filters):
        manifest, results, count = atlas_vector_search(args, question, db, settings, chat_filters.mongo_filter(attempt),
                                                       exact=True, query=query)
        if results:
            break
    return manifest, results, count, {"filters": chat_filters.describe(filters),
                                      "applied_filters": chat_filters.describe(attempt), "relaxed": relaxed}


def run_search(args):  # rank chunks and save the question with its evidence for exercise 3
    question = args.question.strip()  # saved for exercise 3: the answer model sees the question as asked
    if not question:
        raise ValueError("Question must contain text")
    question = (args.query or "").strip() or question  # what is embedded and ranked (self-query: the topic only)
    filters = chat_filters.normalize(args.senders, args.date_from, args.date_to, chat_filters.chat_timezone(args.timezone))
    if chat_filters.active(filters):  # sender/time filters run inside MongoDB's $vectorSearch
        if args.source == "file":
            raise ValueError("Sender/time filters need MongoDB: use --source db (Vectors from: mongodb)")
        client, settings = atlas_connection(args)
        try:
            manifest, results, count, info = filtered_search(args, question, client[settings["db"]], settings, filters)
        finally:
            client.close()
        if info["relaxed"]:
            print(f"RELAXED: no chunk matched the filters; dropped {', '.join(info['relaxed'])}")
        retrieval = retrieval_info(args, "atlas_vector_search_filtered_exact", "mongodb", count)
        return write_output(args, question, manifest, results, {**retrieval, **info})
    if args.source == "file":  # offline path: vectors from build/index.json
        manifest, records = read_index_file(args.index)
        results, count = rank_locally(args, question, manifest, records)
        return write_output(args, question, manifest, results, retrieval_info(args, "cosine_similarity", "file", count))
    client, settings = atlas_connection(args)  # default path: vectors from MongoDB
    try:
        db = client[settings["db"]]
        if args.backend == "atlas":  # server-side ranking
            manifest, results, count = atlas_vector_search(args, question, db, settings)
            method = "atlas_vector_search_exact" if args.exact else "atlas_vector_search_hnsw"
        else:  # vectors from the DB, ranked by your cosine_similarity / select_top_k
            manifest, records = read_index_db(db, settings["collection"])
            results, count = rank_locally(args, question, manifest, records)
            method = "cosine_similarity"
    finally:
        client.close()
    write_output(args, question, manifest, results, retrieval_info(args, method, "mongodb", count))


def retrieval_info(args, method, source, candidate_count):  # run details saved alongside the results
    return {"method": method, "source": source, "top_k": args.top_k,
            "exclude_superseded": args.exclude_superseded, "candidate_count": candidate_count}


def write_output(args, question, manifest, results, retrieval):  # save and print results; the contract for exercise 3
    asked = (getattr(args, "question", None) or "").strip() or question  # the question as asked, for the answer model
    if asked != question:  # --query: the searched text differs (self-query kept only the topic)
        retrieval = {**retrieval, "search_query": question}
    output = {"schema_version": 1, "question": asked, "index_manifest": manifest,
              "results": results, "mode": manifest["embedding_mode"], "retrieval": retrieval}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"MODE: {output['mode']}; METHOD: {retrieval['method']}")
    print(f"CANDIDATES: {retrieval['candidate_count']}; RETURNED: {len(results)}; TOP_K: {args.top_k}")
    for record in results:
        metadata = record["metadata"]
        print(f"RANK {record['rank']} | SCORE {record['score']:.6f} | {metadata['document_id']} v{metadata['version']} | {metadata['status']}")
    print(f"SAVED: {path.resolve()}")
    if output["mode"] == "offline_fixture":
        print("FIXTURE ONLY: ranking checks code flow, not semantic relevance")


def add_filter_arguments(parser):  # chat filters, shared with hybrid_search.py; none given = today's behavior
    parser.add_argument("--query", help="Search this text instead of --question (which is still saved for the answer)")
    parser.add_argument("--senders", help="Chat chunks with any of these senders (comma-separated canonical names)")
    parser.add_argument("--from", dest="date_from", help="Chat chunks overlapping this start: YYYY-MM-DD[THH:MM]")
    parser.add_argument("--to", dest="date_to", help="Chat chunks overlapping this end: YYYY-MM-DD[THH:MM] (a date = whole day)")
    parser.add_argument("--timezone", help="Time zone of --from/--to (default: CHAT_TIMEZONE or Asia/Jerusalem)")


def main():
    parser = argparse.ArgumentParser(description="Exercise 2: retrieval without answer generation")
    parser.add_argument("--question", required=True)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--output", default="build/search-results.json")
    parser.add_argument("--source", choices=("db", "file"), default="db",
                        help="db = load vectors from MongoDB (default); file = load --index from disk")
    parser.add_argument("--backend", choices=("local", "atlas"), default="local",
                        help="local = your cosine/top-k code; atlas = Atlas Vector Search ranks on the server (requires --source db)")
    parser.add_argument("--index", default="../exercise-1-indexing/build/index.json", help="Used with --source file")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434", help="Ollama server for the query embedding")
    parser.add_argument("--allow-fixture", action="store_true")
    parser.add_argument("--exclude-superseded", action="store_true")
    parser.add_argument("--atlas-env", help="Credentials file (default: exercise-1-indexing/atlas-credentials.env or .env)")
    parser.add_argument("--db", help="Override the database name from the credentials file")
    parser.add_argument("--collection", help="Override the collection name from the credentials file")
    parser.add_argument("--vector-index", help="Override the Atlas Vector Search index name")
    parser.add_argument("--exact", action="store_true", help="Atlas backend: exact nearest neighbours (ENN) instead of HNSW")
    parser.add_argument("--ask-uri", action="store_true", help="Type the Atlas connection string privately")
    add_filter_arguments(parser)
    args = parser.parse_args()
    if args.top_k <= 0:
        parser.error("top-k must be positive")
    if args.backend == "atlas" and args.source == "file":
        parser.error("--backend atlas searches MongoDB; it cannot be combined with --source file")
    try:
        run_search(args)
    except Exception as error:  # includes Atlas network and auth errors (already stripped of the URI)
        print(f"STOP: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
