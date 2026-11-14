"""Two optional bonuses; complete them after the core pipeline works.

Bonus 1 - Hybrid Search: a lexical BM25 ranking (a TF-IDF family scoring function)
is computed over the same chunks as the semantic ranking, and the two rankings are
combined with Reciprocal Rank Fusion. RRF uses only positions, so it never assumes
that BM25 scores and cosine scores live on the same scale.

Bonus 2 - Reranking is checked here on fixture scores, without an extra model service.
"""
import math  # log for IDF
import re  # tokenization
from collections import Counter  # term frequencies

# ---------------------------------------------------------------------------
# Bonus 1a - BM25 lexical ranking
# ---------------------------------------------------------------------------
TOKEN = re.compile(r"\w+")  # Unicode words: Hebrew, Latin and numbers such as 401
HEBREW_PREFIXES = "והבלמשכ"  # one-letter prefixes attached to Hebrew words (and, the, in, to, from, that, as)


def tokenize(text):  # split text into the terms BM25 counts
    """Return a list of lowercase terms.

    Hebrew attaches prefixes to words ("בהתחברות" = "in" + "the" + "login"), so a word
    with a likely prefix also emits a variant with up to two prefix letters removed.
    Query and documents use the same function, so the extra variants match consistently.
    """
    terms = []
    for word in TOKEN.findall(text.lower()):
        terms.append(word)  # the exact word always counts, so identifiers like "401" or "esc" match as-is
        stripped = word
        for _ in range(2):  # strip at most two prefix letters ("וה", "בה", "שב"...)
            if len(stripped) > 3 and stripped[0] in HEBREW_PREFIXES:  # keep short words intact
                stripped = stripped[1:]
                terms.append(stripped)  # e.g. "בהתחברות" -> "התחברות" -> "תחברות"
    return terms


class BM25:  # Okapi BM25 over a fixed set of chunks
    """Score chunks for a query with Okapi BM25.

    For each query term t:  IDF(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len / avg_len))
      IDF(t) = ln(1 + (N - df + 0.5) / (df + 0.5))   (always positive; Lucene's variant)
      tf     = how many times t occurs in the chunk
      k1     = term-frequency saturation: the 5th repeat of a word adds far less than the 1st
      b      = length normalization: long chunks don't win just by containing more words
    Plain TF-IDF grows linearly with tf and ignores chunk length; BM25 fixes both.
    """

    def __init__(self, records, k1=1.5, b=0.75):  # index the chunks once; score many queries
        if not records:
            raise ValueError("BM25 needs at least one record")
        if k1 < 0 or not 0 <= b <= 1:
            raise ValueError("k1 must be >= 0 and b must be between 0 and 1")
        self.k1, self.b = k1, b
        self.ids = [record["chunk_id"] for record in records]
        self.term_counts = [Counter(tokenize(record["text"])) for record in records]  # tf per chunk
        self.lengths = [sum(counts.values()) for counts in self.term_counts]  # chunk length in terms
        self.avg_length = sum(self.lengths) / len(self.lengths) or 1  # avoid division by zero for empty texts
        document_frequency = Counter(term for counts in self.term_counts for term in counts)  # chunks containing each term
        n = len(records)
        self.idf = {term: math.log(1 + (n - df + 0.5) / (df + 0.5)) for term, df in document_frequency.items()}

    def scores(self, query):  # {chunk_id: score} for every chunk; 0 when no query term occurs
        query_terms = set(tokenize(query))  # a repeated query word should not double its weight
        result = {}
        for chunk_id, counts, length in zip(self.ids, self.term_counts, self.lengths):
            norm = self.k1 * (1 - self.b + self.b * length / self.avg_length)  # length-normalized saturation point
            result[chunk_id] = sum(self.idf[t] * counts[t] * (self.k1 + 1) / (counts[t] + norm)
                                   for t in query_terms if counts[t])  # terms unseen in the chunk contribute nothing
        return result

    def rank(self, query):  # chunk IDs from best to worst; chunks without any match are left out
        scores = self.scores(query)
        matched = [chunk_id for chunk_id, score in scores.items() if score > 0]
        return sorted(matched, key=lambda chunk_id: (-scores[chunk_id], chunk_id)), scores  # ties by chunk_id, like select_top_k


# ---------------------------------------------------------------------------
# Bonus 1b - Reciprocal Rank Fusion
# ---------------------------------------------------------------------------
def reciprocal_rank_fusion(rankings, constant=60):  # combine several rankings by each chunk's position in them
    """Input: a list of rankings; each ranking is a list of chunk IDs from best to worst.

    Output: a list of {'chunk_id': str, 'score': float}, sorted by RRF score.
    A chunk at position rank contributes 1/(constant+rank), where rank starts at 1.
    A chunk missing from a ranking gets zero from it. Ties are sorted by chunk_id.
    """
    if constant < 0:
        raise ValueError("constant must be non-negative")
    totals = {}
    for ranking in rankings:
        if len(set(ranking)) != len(ranking):  # a chunk can hold only one position in a ranking
            raise ValueError("A ranking contains duplicate chunk IDs")
        for rank, chunk_id in enumerate(ranking, 1):
            totals[chunk_id] = totals.get(chunk_id, 0.0) + 1 / (constant + rank)
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return [{"chunk_id": chunk_id, "score": score} for chunk_id, score in ordered]


# ---------------------------------------------------------------------------
# Bonus 2 - Reranking
# ---------------------------------------------------------------------------
def rerank_candidates(candidates, rerank_scores, k):  # reorder only candidates the first stage already returned
    """Input: candidates with chunk_id and text, and a dict chunk_id -> reranker score.

    Add rerank_score and choose up to k candidates in descending order, without modifying the input.
    Require a score for every candidate and reject an ID that is not a candidate; keep source and text.
    The sample scores are a fixture for checking the stage wiring and source traceability.
    """
    # TODO BONUS 2: reranking changes the order inside the candidate pool that was already returned.
    # Also check a case where the desired evidence is missing from the pool: it must not be added from outside.
    raise NotImplementedError("BONUS 2: implement rerank_candidates")
