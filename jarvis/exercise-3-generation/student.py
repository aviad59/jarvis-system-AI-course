# The parts students build: context assembly, and the citation-check bonus.
# The other files handle JSON loading, the provider connection and the viewer, so this file focuses on Generation.


def assemble_context(question, results, max_chars):  # question + ranked chunks + a character budget for source text
    """Return {'question': question, 'sources': [{'chunk_id', 'text', 'metadata'}, ...]}.

    Sources follow rank order, appear once, and are copied exactly. Only whole chunks
    that fit the remaining budget are included; a chunk is never cut in the middle.
    The budget counts characters to keep the exercise simple; it is not a token count.
    Raises ValueError when not even one chunk fits, instead of sending an empty request.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    sources, seen, used = [], set(), 0
    for row in sorted(results, key=lambda row: row["rank"]):  # best evidence first, whatever order the list arrived in
        if row["chunk_id"] in seen:  # the same chunk twice would waste budget and duplicate a citation target
            continue
        if used + len(row["text"]) > max_chars:  # doesn't fit whole: skip it, a smaller lower-ranked chunk may still fit
            continue
        seen.add(row["chunk_id"])
        used += len(row["text"])
        source = {"chunk_id": row["chunk_id"], "text": row["text"], "metadata": row["metadata"]}  # exact text and source
        if row.get("messages"):  # a chat chunk: text is "Sender [time]: ..."; messages say who said what, structured
            source["messages"] = row["messages"]
        sources.append(source)
    if not sources:
        raise ValueError(f"No source fits in max_chars={max_chars}; increase --max-chars or use smaller chunks")
    return {"question": question, "sources": sources}


def invalid_citation_ids(answer, sources):  # bonus 1: compare citations only with the sources actually sent
    """Return a sorted list of unique cited IDs that were not in sources.

    An ID that exists in the index but was not sent to the model is still invalid.
    A valid ID does not prove the claim is true or supported; a human still checks that.
    """
    sent = {source["chunk_id"] for source in sources}
    cited = {chunk_id for claim in answer["claims"] for chunk_id in claim["citation_ids"]}
    return sorted(cited - sent)
