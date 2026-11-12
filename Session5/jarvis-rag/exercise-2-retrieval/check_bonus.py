"""בדיקות בונוסים עם מספרים מומצאים, ללא קריאת מודל reranker."""  # משלימים את ביטוי הנתונים של check_bonus תוך שמירה על המבנה המוצג
import copy  # טוענים כלי עבור בדיקת שמירת הקלט המקורי
import unittest  # טוענים כלי עבור בדיקת חוזי הפונקציות ללא מודל
from bonus_ranking import BM25, reciprocal_rank_fusion, rerank_candidates  # bonus functions, tested separately from the core


class BonusChecks(unittest.TestCase):  # מקבצים בדיקות של אותו חוזה התנהגות כדי להריצן יחד
    def test_rrf_combines_different_orders(self):  # מגדירים מקרה בדיקה: rrf combines different orders
        result = reciprocal_rank_fusion([["A", "B", "C"], ["B", "C", "A"]], 60)  # שומרים את תוצאת הפעולה לצורך אימות או המשך עיבוד
        self.assertEqual([item["chunk_id"] for item in result], ["B", "A", "C"])  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
        self.assertAlmostEqual(result[0]["score"], 1 / 62 + 1 / 61)  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל

    def test_rrf_missing_ids_contribute_zero(self):  # מגדירים מקרה בדיקה: rrf missing ids contribute zero
        result = reciprocal_rank_fusion([["A"], ["B", "A"]], 60)  # שומרים את תוצאת הפעולה לצורך אימות או המשך עיבוד
        self.assertEqual(result[0]["chunk_id"], "A")  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
        self.assertAlmostEqual(result[0]["score"], 1 / 61 + 1 / 62)  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל

    def test_reranking_preserves_source_and_text(self):  # מגדירים מקרה בדיקה: reranking preserves source and text
        candidates = [{"chunk_id": "A", "text": "historical evidence", "metadata": {"source": "old.md"}},  # מכינים את מאגר הקטעים המועמדים לדירוג
                      {"chunk_id": "B", "text": "current procedure", "metadata": {"source": "current.md"}}]  # משלימים את ביטוי הנתונים של test_reranking_preserves_source_and_text תוך שמירה על המבנה המוצג
        before = copy.deepcopy(candidates)  # שומרים עותק של הקלט כדי לבדוק שהפונקציה לא שינתה אותו
        result = rerank_candidates(candidates, {"A": 0.2, "B": 0.9}, 1)  # שומרים את תוצאת הפעולה לצורך אימות או המשך עיבוד
        self.assertEqual(result[0]["chunk_id"], "B")  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
        self.assertEqual(result[0]["metadata"]["source"], "current.md")  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
        self.assertEqual(result[0]["text"], "current procedure")  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
        self.assertEqual(candidates, before)  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל

    def test_reranking_cannot_invent_missing_candidate(self):  # מגדירים מקרה בדיקה: reranking cannot invent missing candidate
        with self.assertRaises(ValueError):  # פותחים משאב לזמן הפעולה וסוגרים אותו בסיומה
            rerank_candidates([{"chunk_id": "A"}], {"A": 0.2, "MISSING": 0.9}, 1)  # משלימים את ביטוי הנתונים של test_reranking_cannot_invent_missing_candidate תוך שמירה על המבנה המוצג

    def test_reranking_requires_scores_for_all_candidates(self):  # מגדירים מקרה בדיקה: reranking requires scores for all candidates
        with self.assertRaises(ValueError):  # פותחים משאב לזמן הפעולה וסוגרים אותו בסיומה
            rerank_candidates([{"chunk_id": "A"}, {"chunk_id": "B"}], {"A": 0.2}, 1)  # משלימים את ביטוי הנתונים של test_reranking_requires_scores_for_all_candidates תוך שמירה על המבנה המוצג


class BM25Checks(unittest.TestCase):  # bonus 1: the lexical ranking that feeds RRF
    RECORDS = [{"chunk_id": "A", "text": "HTTP 401 errors after deploy"},
               {"chunk_id": "B", "text": "notification delay in the worker queue"},
               {"chunk_id": "C", "text": "HTTP 5xx errors in the delivery service and more words to make it long"}]

    def test_exact_identifier_ranks_first(self):  # an exact code like 401 is where lexical search shines
        ranking, scores = BM25(self.RECORDS).rank("401")
        self.assertEqual(ranking, ["A"])  # chunks with no matching word are left out of the ranking
        self.assertEqual(scores["B"], 0)

    def test_rare_terms_weigh_more_than_common_ones(self):  # IDF: "http" is in two chunks, "deploy" in one
        bm25 = BM25(self.RECORDS)
        self.assertGreater(bm25.idf["deploy"], bm25.idf["http"])

    def test_length_normalization(self):  # with b=1 the shorter chunk wins on the same shared term
        ranking, _ = BM25(self.RECORDS, b=1.0).rank("errors")
        self.assertEqual(ranking, ["A", "C"])

    def test_term_frequency_saturates(self):  # the 10th repeat adds less than the 2nd
        bm25 = BM25([{"chunk_id": str(n), "text": "cache " * n + "filler"} for n in (1, 2, 10)] + [{"chunk_id": "x", "text": "other"}], b=0)
        scores = bm25.scores("cache")
        self.assertGreater(scores["2"] - scores["1"], (scores["10"] - scores["2"]) / 8)

    def test_hebrew_prefix_variants_match(self):  # "בהתחברות" in a query should find "התחברות" in a chunk
        ranking, _ = BM25([{"chunk_id": "A", "text": "שגיאות התחברות"}, {"chunk_id": "B", "text": "עיכוב בהתראות"}]).rank("תקלה בהתחברות")
        self.assertEqual(ranking[0], "A")

    def test_hybrid_fusion_of_semantic_and_bm25(self):  # RRF combines the two rankings by position only
        lexical, _ = BM25(self.RECORDS).rank("HTTP 401")
        fused = reciprocal_rank_fusion([["C", "A", "B"], lexical], 60)
        self.assertEqual(fused[0]["chunk_id"], "A")  # 2nd semantically + 1st lexically beats C (1st semantically, absent lexically)
        self.assertAlmostEqual(fused[0]["score"], 1 / 62 + 1 / 61)


if __name__ == "__main__":  # מפעילים את התוכנית כאשר מריצים את הקובץ עצמו
    unittest.main(verbosity=2)  # מריצים את כל בדיקות הקובץ ומציגים תוצאה לכל בדיקה
