"""Bonus 1 - Hybrid Search: semantic ranking + BM25 ranking, combined with RRF.

Both rankings run over the same chunks (loaded from MongoDB by default, like search.py),
so they share chunk IDs. Each retriever contributes its top --pool chunks; RRF fuses
the two lists by position and the fused top-k is saved in the same format as search.py,
so exercise 3 can use it.

  python hybrid_search.py --question "HTTP 401"
  python hybrid_search.py --demo          (compares the three README question types)
"""
import argparse  # command-line options
import sys  # exit codes and error output
from bonus_ranking import BM25, reciprocal_rank_fusion  # the bonus 1 building blocks
from retrieval import cosine_similarity, select_top_k  # the core semantic ranking functions
from search import atlas_connection, read_index_db, read_index_file, query_embedding, retrieval_info, write_output

DEMO_QUESTIONS = (  # README step 4: an exact code, a paraphrase, and a question that needs several documents
    "HTTP 401",
    "המשתמשים מקבלים הודעות מאוחר מהרגיל, מה לבדוק?",
    "מה ההבדל בין תקלת ההתחברות של ספטמבר לבין התקלה הנוכחית?",
)


def load_records(args):  # the same chunks and vectors that search.py ranks
    if args.source == "file":
        return read_index_file(args.index)
    client, settings = atlas_connection(args)
    try:
        return read_index_db(client[settings["db"]], settings["collection"])
    finally:
        client.close()


def hybrid(question, manifest, records, bm25, args):  # three rankings for one question
    candidates = [record for record in records  # same optional version filter as search.py
                  if not (args.exclude_superseded and record["metadata"]["status"] == "superseded")]
    allowed = {record["chunk_id"] for record in candidates}
    query = query_embedding(question, manifest, args)
    semantic = [{"chunk_id": r["chunk_id"], "score": cosine_similarity(query, r["embedding"])} for r in candidates]
    semantic_ranking = [item["chunk_id"] for item in select_top_k(semantic, args.pool)]  # best --pool chunks by meaning
    lexical_ranking, bm25_scores = bm25.rank(question)
    lexical_ranking = [chunk_id for chunk_id in lexical_ranking if chunk_id in allowed][:args.pool]  # best --pool chunks by words
    fused = reciprocal_rank_fusion([semantic_ranking, lexical_ranking], args.rrf_constant)[:args.top_k]
    return {"semantic": semantic_ranking, "lexical": lexical_ranking, "fused": fused,
            "semantic_scores": {item["chunk_id"]: item["score"] for item in semantic}, "bm25_scores": bm25_scores}


def label(chunk_id, by_id):  # short, readable name for a chunk: DOCUMENT-ID#n
    return f"{by_id[chunk_id]['metadata']['document_id']}#{chunk_id[-6:]}"


def print_comparison(question, ranks, by_id, top_k):  # side-by-side view of what each ranking found
    print(f"\nQUESTION: {question}")
    print(f"{'#':<3}{'SEMANTIC (cosine)':<34}{'LEXICAL (BM25)':<34}{'HYBRID (RRF)':<34}")
    columns = ([(c, ranks["semantic_scores"][c]) for c in ranks["semantic"]],  # (chunk_id, score) per ranking
               [(c, ranks["bm25_scores"][c]) for c in ranks["lexical"]],
               [(item["chunk_id"], item["score"]) for item in ranks["fused"]])
    for i in range(top_k):
        cells = [f"{label(col[i][0], by_id)} {col[i][1]:.4f}" if i < len(col) else "-" for col in columns]
        print(f"{i + 1:<3}" + "".join(f"{cell:<34}" for cell in cells))
    only_lexical = [label(c["chunk_id"], by_id) for c in ranks["fused"] if c["chunk_id"] not in ranks["semantic"][:top_k]]
    if only_lexical:  # what fusion changed compared to semantic search alone
        print(f"HYBRID ADDED vs semantic top-{top_k}: {', '.join(only_lexical)}")
    if not ranks["lexical"]:
        print("BM25: no query word occurs in any chunk; hybrid equals semantic")


def main():
    parser = argparse.ArgumentParser(description="Bonus 1: hybrid search (semantic + BM25 + RRF)")
    questions = parser.add_mutually_exclusive_group(required=True)
    questions.add_argument("--question")
    questions.add_argument("--demo", action="store_true", help="Compare the three README question types (prints only)")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--pool", type=int, default=10, help="How many chunks each retriever contributes to RRF")
    parser.add_argument("--rrf-constant", type=int, default=60)
    parser.add_argument("--k1", type=float, default=1.5, help="BM25 term-frequency saturation")
    parser.add_argument("--b", type=float, default=0.75, help="BM25 length normalization (0 = off, 1 = full)")
    parser.add_argument("--output", default="build/search-results.json")
    parser.add_argument("--source", choices=("db", "file"), default="db")
    parser.add_argument("--index", default="../exercise-1-indexing/build/index.json")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--ask-key", action="store_true")
    parser.add_argument("--allow-fixture", action="store_true")
    parser.add_argument("--exclude-superseded", action="store_true")
    parser.add_argument("--atlas-env")
    parser.add_argument("--db")
    parser.add_argument("--collection")
    parser.add_argument("--ask-uri", action="store_true")
    args = parser.parse_args()
    args.vector_index = None  # atlas_connection reads it; hybrid search ranks locally
    if args.top_k <= 0 or args.pool < args.top_k:
        parser.error("top-k must be positive and --pool must be at least --top-k")
    try:
        manifest, records = load_records(args)
        bm25 = BM25(records, args.k1, args.b)  # built once, reused for every question
        by_id = {record["chunk_id"]: record for record in records}
        for question in (DEMO_QUESTIONS if args.demo else [args.question.strip()]):
            if not question:
                raise ValueError("Question must contain text")
            ranks = hybrid(question, manifest, records, bm25, args)
            print_comparison(question, ranks, by_id, args.top_k)
        if args.demo:
            return 0
        results = []  # same record shape as search.py, plus the score from each retriever
        for rank, item in enumerate(ranks["fused"], 1):
            record = by_id[item["chunk_id"]]
            results.append({"rank": rank, "chunk_id": record["chunk_id"], "text": record["text"], "metadata": record["metadata"],
                             "score": item["score"],
                             "semantic_score": ranks["semantic_scores"].get(record["chunk_id"]),
                             "bm25_score": ranks["bm25_scores"].get(record["chunk_id"], 0.0)})
        info = retrieval_info(args, "hybrid_rrf(cosine+bm25)", args.source if args.source == "file" else "mongodb", len(records))
        info.update({"pool": args.pool, "rrf_constant": args.rrf_constant, "bm25": {"k1": args.k1, "b": args.b}})
        print()
        write_output(args, question, manifest, results, info)
    except Exception as error:
        print(f"STOP: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
