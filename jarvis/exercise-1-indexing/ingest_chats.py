"""Migration: parse the WhatsApp exports in the vault, embed their chunks and upsert them into MongoDB.

  python ingest_chats.py --dry-run                 # parse and chunk only: prints messages and stats, writes nothing
  python ingest_chats.py                           # embed (Ollama) + upsert into Atlas, in batches
  python ingest_chats.py --file chat.md --show 20  # one export, more sample messages in the dry run

Idempotent: chunks are keyed on (chat_file, chunk_index); a rerun updates them in place, reuses vectors
whose input did not change (same model digest), and deletes chunks that no longer exist. Without --file,
chat files that are no longer in the vault are removed from the collection too.
The embedding model and input format come from the collection's manifest, so chat vectors live in the
same space as the note vectors (and the question). Credentials: atlas-credentials.env, as everywhere else.
"""
import argparse  # command-line options
import os  # JARVIS_VAULT
import sys  # exit codes
from pathlib import Path  # vault path

import chat_parser  # parse + chunk
from index_support import BASE, VAULT_INCLUDE, digest, embed_texts, load_vault, resolve_model, validate_vectors

DEFAULT_VAULT = Path(os.environ.get("JARVIS_VAULT") or Path.home() / "Documents" / "Obsidian Vault")


def chat_records(args):  # [(document, [records])] for the chat exports in scope
    documents = [d for d in load_vault(args.vault, VAULT_INCLUDE) if d.get("kind") == "chat"]
    if args.file:
        wanted = {name.replace("\\", "/") for name in args.file}
        documents = [d for d in documents if d["path"] in wanted]
        missing = wanted - {d["path"] for d in documents}
        if missing:
            raise ValueError(f"Not a chat export in the vault: {', '.join(sorted(missing))}")
    if not documents:
        raise ValueError(f"No WhatsApp chat exports found in {args.vault}")
    tz = chat_parser.chat_timezone(args.timezone)
    return [(d, chat_parser.index_records(d, args.max_chars, tz)) for d in documents]


def dry_run(found, show):
    for document, records in found:
        messages = [m for r in records for m in r["chat"]["messages"]]
        sizes = sorted(len(r["text"]) for r in records)
        counts = {}
        for m in messages:
            counts[m["sender"]] = counts.get(m["sender"], 0) + 1
        print(f"\nCHAT {document['path']} ({document['title']})")
        print(f"  messages={len(messages)} chunks={len(records)} display_text chars min/median/max="
              f"{sizes[0]}/{sizes[len(sizes) // 2]}/{sizes[-1]}")
        print(f"  first={messages[0]['ts']} last={messages[-1]['ts']} (UTC)")
        print("  participants: " + ", ".join(f"{name} ({n})" for name, n in sorted(counts.items(), key=lambda kv: -kv[1])))
        print(f"  first {min(show, len(messages))} parsed messages:")
        for m in messages[:show]:
            reply = f" ↩ {m['reply_to']['sender']}" if m.get("reply_to") else ""
            print(f"    {m['ts']} | {m['sender']}{reply} | {m['text'][:80]!r}")
        print("  chunk 0 embed_text:")
        print("    " + records[0]["embedding_text"][:300].replace("\n", "\n    "))
        print("  chunk 0 display_text:")
        print("    " + records[0]["text"][:300].replace("\n", "\n    "))
    print("\nDRY RUN: nothing was embedded or written")


def migrate(found, args):
    import atlas_upload  # pymongo is needed only here
    import chat_store
    settings, problem = atlas_upload.atlas_settings(BASE, args.atlas_env)
    if problem:
        raise ValueError(problem)
    client = atlas_upload.connect(settings["uri"])
    try:
        db = client[settings["db"]]
        collection = db[settings["collection"]]
        manifest = db["index_manifests"].find_one({"_id": settings["collection"]}, {"_id": 0})
        model, model_digest = resolve_model(args.base_url, manifest["model"] if manifest else args.model)
        if manifest and manifest.get("model_digest") != model_digest:
            raise ValueError(f"The installed {model} differs from the one that embedded the collection "
                             "(model_digest changed); rebuild the whole index instead")
        document_format = manifest["document_format"] if manifest else "embeddinggemma"
        print(f"SOURCE: db={settings['db']} collection={settings['collection']} model={model} format={document_format}")
        records = [r for _, rs in found for r in rs]
        texts = [chat_parser.embedding_input(r["metadata"]["title"], r["embedding_text"], document_format) for r in records]
        cache = {} if args.force_embed else chat_store.existing_vectors(collection, {d["path"] for d, _ in found}, model_digest)
        docs, reused, inserted, updated = [], 0, 0, 0
        meta = {"model": model, "model_digest": model_digest}
        chat_store.ensure_indexes(collection)  # the unique (chat_file, chunk_index) key guards every write below
        for start in range(0, len(records), args.batch_size):  # embed a batch, write it, go on
            batch, inputs = records[start:start + args.batch_size], texts[start:start + args.batch_size]
            missing = [i for i, text in enumerate(inputs) if digest(text) not in cache]
            fresh = embed_texts(args.base_url, model, [inputs[i] for i in missing]) if missing else []
            vectors = [cache.get(digest(text)) for text in inputs]
            for i, vector in zip(missing, fresh):
                vectors[i] = vector
            reused += len(batch) - len(missing)
            dimension = validate_vectors(vectors)
            if manifest and dimension != manifest["dimensions"]:
                raise ValueError(f"Vectors have {dimension} dimensions, the collection {manifest['dimensions']}")
            batch_docs = []
            for record, text, vector in zip(batch, inputs, vectors):
                batch_docs.append(chat_store.mongo_doc({**record, "embedding": vector, "embedding_input_sha256": digest(text)}, meta))
            added, changed = chat_store.upsert(collection, batch_docs)
            inserted, updated = inserted + added, updated + changed
            docs.extend(batch_docs)
            print(f"EMBEDDED+UPSERTED: {min(start + args.batch_size, len(records))}/{len(records)} (reused so far: {reused})")
        result = {"inserted": inserted, "updated": updated, **chat_store.finish(db, collection, docs, all_files=not args.file)}
        if not manifest:  # first upload into an empty collection: searches need to know the model
            manifest = {"embedding_mode": "ollama", "model": model, "model_digest": model_digest, "dimensions": dimension,
                        "document_format": document_format, "document_count": 0,
                        "chunk_count": collection.count_documents({"kind": {"$ne": "chat"}}),
                        "chunking": {"max_chars": args.max_chars, "structured": False}}
        manifest["chat_chunk_count"] = result["chunks"]
        db["index_manifests"].replace_one({"_id": settings["collection"]}, {"_id": settings["collection"], **manifest}, upsert=True)
        print(f"CHATS: chunks={result['chunks']} inserted={result['inserted']} updated={result['updated']} "
              f"removed={result['removed']} reused_vectors={reused} participants={result['participants']}")
        atlas_upload.ensure_vector_index(collection, settings["vector_index"], manifest["dimensions"], args.wait)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description="Parse, embed and upsert the vault's WhatsApp exports into MongoDB")
    parser.add_argument("--vault", default=str(DEFAULT_VAULT))
    parser.add_argument("--file", action="append", help="Only this export (path inside the vault); repeatable")
    parser.add_argument("--dry-run", action="store_true", help="Print parsed messages and chunk stats; write nothing")
    parser.add_argument("--show", type=int, default=10, help="Dry run: how many parsed messages to print")
    parser.add_argument("--max-chars", type=int, default=900, help="Chunk size limit (same as build_index.py)")
    parser.add_argument("--timezone", help=f"Time zone of the export's times (default: CHAT_TIMEZONE or {chat_parser.DEFAULT_TIMEZONE})")
    parser.add_argument("--batch-size", type=int, default=8, help="Chunks per embedding request / write")
    parser.add_argument("--model", default="embeddinggemma", help="Used only when the collection has no manifest yet")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--force-embed", action="store_true", help="Re-embed every chunk even if its input is unchanged")
    parser.add_argument("--atlas-env")
    parser.add_argument("--wait", type=int, default=180, help="Seconds to wait for the vector index")
    args = parser.parse_args()
    if args.timezone:
        os.environ["CHAT_TIMEZONE"] = args.timezone
    try:
        found = chat_records(args)
        if args.dry_run:
            dry_run(found, args.show)
        else:
            migrate(found, args)
    except Exception as error:
        message = str(error)
        try:
            import atlas_upload
            settings, _ = atlas_upload.atlas_settings(BASE, args.atlas_env)
            message = atlas_upload.safe_error(error, settings)  # never print the URI
        except Exception:
            pass
        print(f"STOP: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
