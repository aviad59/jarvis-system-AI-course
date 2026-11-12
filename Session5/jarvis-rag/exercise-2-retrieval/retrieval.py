"""Ranking functions for the Retrieval exercise.

This file only defines functions. search.py embeds the question, calls these
functions and saves the selected chunks with their sources. No LLM writes an answer yet.
"""
import math  # sqrt for vector length, isfinite for input validation


def cosine_similarity(query_vector, document_vector):  # a match score: dot product normalized by vector lengths
    """Return one number: the dot product divided by the product of the vector lengths.

    Both vectors must have the same number of dimensions, finite values and a positive length.
    Raise ValueError on invalid input. Do not modify the input lists.
    """
    if len(query_vector) != len(document_vector):  # different dimensions cannot be compared
        raise ValueError(f"Dimension mismatch: {len(query_vector)} != {len(document_vector)}")
    if not query_vector:  # empty vectors have no direction
        raise ValueError("Vectors must not be empty")
    if not all(math.isfinite(x) for x in query_vector) or not all(math.isfinite(x) for x in document_vector):
        raise ValueError("Vectors must contain only finite numbers")  # NaN or inf would poison the score
    dot = sum(q * d for q, d in zip(query_vector, document_vector))  # how much the vectors point the same way
    query_norm = math.sqrt(sum(q * q for q in query_vector))  # length of the query vector
    document_norm = math.sqrt(sum(d * d for d in document_vector))  # length of the document vector
    if query_norm == 0 or document_norm == 0:  # a zero vector has no direction, so cosine is undefined
        raise ValueError("Vectors must have a positive length")
    # The score describes geometric similarity in [-1, 1]; it is not a probability that the answer is correct.
    return dot / (query_norm * document_norm)


def select_top_k(scored_records, k):  # pick the records with the highest scores
    """Return up to k records, from highest to lowest score, without modifying the input.

    Each record has score and chunk_id, alongside the text and metadata.
    On ties, sort by chunk_id ascending so runs are reproducible.
    Raise ValueError if k is not a positive integer.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:  # bool is a subclass of int, so reject it explicitly
        raise ValueError(f"k must be a positive integer, got {k!r}")
    ranked = sorted(scored_records, key=lambda record: (-record["score"], record["chunk_id"]))  # new list; input untouched
    return ranked[:k]  # shorter input simply returns every record, in order
