"""MongoDB storage for chat chunks: the documents, their regular indexes and the participants collection.

Chat chunks live in the same collection as the note chunks (one $vectorSearch covers both), marked
kind="chat". Their unique key is (chat_file, chunk_index); _id = "chat:<chat_file>:<chunk_index>".
Used by ingest_chats.py (the migration) and by atlas_upload.sync_index (the normal vault sync).
"""
from chat_parser import from_iso

PARTICIPANTS = "participants"  # one document per canonical sender name
BATCH_SIZE = 8


def mongo_doc(record, manifest):  # an index record (chat kind, with its embedding) -> the MongoDB chat document
    chat = record["chat"]
    return {
        "_id": record["chunk_id"], "chunk_id": record["chunk_id"], "kind": "chat",
        "chat_file": chat["chat_file"], "chunk_index": chat["chunk_index"],
        "embed_text": record["embedding_text"],  # contents only: the embedding is computed from this
        "display_text": record["text"],  # "Sender [date time]: text": what the answer model reads
        "text": record["text"],  # the field every reader of the collection already uses
        "messages": [{**m, "ts": from_iso(m["ts"])} for m in chat["messages"]],
        "participants": chat["participants"],
        "start_ts": from_iso(chat["start_ts"]), "end_ts": from_iso(chat["end_ts"]),
        "embedding": record["embedding"], "embedding_model": manifest["model"],
        "embedding_model_digest": manifest.get("model_digest"),
        "embedding_input_sha256": record["embedding_input_sha256"],
        "metadata": record["metadata"], "start": record["start"], "end": record["end"],
        "document_sha256": record["document_sha256"],
    }


def upsert(collection, docs):  # idempotent write keyed on (chat_file, chunk_index); returns (inserted, updated)
    from pymongo import UpdateOne
    if not docs:
        return 0, 0
    operations = [UpdateOne({"chat_file": d["chat_file"], "chunk_index": d["chunk_index"]},
                            {"$set": {k: v for k, v in d.items() if k != "_id"}, "$setOnInsert": {"_id": d["_id"]}},
                            upsert=True) for d in docs]
    result = collection.bulk_write(operations, ordered=False)
    return result.upserted_count, result.modified_count


def prune(collection, chunk_counts, all_files=False):
    """Delete chat chunks past the end of each file (chunk_counts = {chat_file: n}).

    all_files=True: chunk_counts lists every chat that should exist, so other chat files are deleted too.
    """
    removed = 0
    for chat_file, count in chunk_counts.items():
        removed += collection.delete_many({"kind": "chat", "chat_file": chat_file, "chunk_index": {"$gte": count}}).deleted_count
    if all_files:
        removed += collection.delete_many({"kind": "chat", "chat_file": {"$nin": list(chunk_counts)}}).deleted_count
    return removed


def ensure_indexes(collection):  # regular indexes behind the filters (the vector index declares them too)
    collection.create_index([("chat_file", 1), ("chunk_index", 1)], name="chat_file_chunk_index", unique=True,
                            partialFilterExpression={"kind": "chat"})
    collection.create_index([("participants", 1), ("start_ts", 1)], name="participants_start_ts")
    collection.create_index([("start_ts", 1), ("end_ts", 1)], name="start_ts_end_ts")


def refresh_participants(db, collection):  # rebuild the participants collection from the stored messages
    from pymongo import ReplaceOne
    rows = list(collection.aggregate([
        {"$match": {"kind": "chat"}},
        {"$unwind": "$messages"},
        {"$group": {"_id": "$messages.sender", "message_count": {"$sum": 1}, "chat_files": {"$addToSet": "$chat_file"},
                    "first_ts": {"$min": "$messages.ts"}, "last_ts": {"$max": "$messages.ts"}}},
    ]))
    target = db[PARTICIPANTS]
    if rows:
        target.bulk_write([ReplaceOne({"_id": r["_id"]}, {**r, "chat_files": sorted(r["chat_files"])}, upsert=True)
                           for r in rows])
    target.delete_many({"_id": {"$nin": [r["_id"] for r in rows]}})
    return len(rows)


def load_participants(db):  # canonical names, most active first
    return [row["_id"] for row in db[PARTICIPANTS].find({}, {"_id": 1}).sort([("message_count", -1), ("_id", 1)])]


def existing_vectors(collection, chat_files, model_digest):  # {embedding_input_sha256: vector} that can be reused
    query = {"kind": "chat", "chat_file": {"$in": list(chat_files)}, "embedding_model_digest": model_digest}
    return {d["embedding_input_sha256"]: d["embedding"]
            for d in collection.find(query, {"embedding_input_sha256": 1, "embedding": 1})
            if d.get("embedding_input_sha256") and d.get("embedding")}


def finish(db, collection, docs, all_files=False):  # after all docs are written: drop stale chunks, refresh participants
    counts = {}
    for d in docs:
        counts[d["chat_file"]] = max(counts.get(d["chat_file"], 0), d["chunk_index"] + 1)
    removed = prune(collection, counts, all_files)
    return {"removed": removed, "participants": refresh_participants(db, collection),
            "chunks": collection.count_documents({"kind": "chat"})}


def sync(db, collection, docs, all_files=False, batch_size=BATCH_SIZE):
    """Write chat docs in batches, drop stale chunks, refresh participants. Returns counts."""
    ensure_indexes(collection)  # first, so the unique key guards the writes below
    inserted = updated = 0
    for start in range(0, len(docs), batch_size):
        a, b = upsert(collection, docs[start:start + batch_size])
        inserted, updated = inserted + a, updated + b
    return {"inserted": inserted, "updated": updated, **finish(db, collection, docs, all_files)}
