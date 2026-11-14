"""בדיקות מספריות לדירוג ולבחירה, ללא מודל וללא קריאת API."""  # משלימים את ביטוי הנתונים של check_exercise תוך שמירה על המבנה המוצג
import copy  # טוענים כלי עבור בדיקת שמירת הקלט המקורי
import math  # טוענים כלי עבור חישוב אורכי וקטורים ובדיקת מספרים סופיים
import unittest  # טוענים כלי עבור בדיקת חוזי הפונקציות ללא מודל
from retrieval import cosine_similarity, select_top_k  # משתמשים בשתי פונקציות הדירוג שהלומדים בונים


class RetrievalChecks(unittest.TestCase):  # מקבצים בדיקות של אותו חוזה התנהגות כדי להריצן יחד
    def test_known_cosine_and_scaling(self):  # מגדירים מקרה בדיקה: known cosine and scaling
        self.assertAlmostEqual(cosine_similarity([1, 0], [0.8, 0.6]), 0.8)  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
        self.assertAlmostEqual(cosine_similarity([1, 0], [1.6, 1.2]), 0.8)  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
        self.assertAlmostEqual(cosine_similarity([1, 0], [-1, 0]), -1.0)  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל

    def test_invalid_vectors_are_rejected(self):  # מגדירים מקרה בדיקה: invalid vectors are rejected
        for left, right in [([0, 0], [1, 0]), ([1], [1, 2]), ([], []), ([math.nan], [1])]:  # עוברים לפי הסדר על left, right ומטפלים בכל פריט בנפרד
            with self.subTest(left=left, right=right), self.assertRaises(ValueError):  # פותחים משאב לזמן הפעולה וסוגרים אותו בסיומה
                cosine_similarity(left, right)  # משלימים את ביטוי הנתונים של test_invalid_vectors_are_rejected תוך שמירה על המבנה המוצג

    def test_top_three_from_four_candidates(self):  # מגדירים מקרה בדיקה: top three from four candidates
        candidates = [{"chunk_id": "A", "score": 0.2}, {"chunk_id": "B", "score": 0.8},  # מכינים את מאגר הקטעים המועמדים לדירוג
                      {"chunk_id": "C", "score": -0.1}, {"chunk_id": "D", "score": 0.5}]  # משלימים את ביטוי הנתונים של test_top_three_from_four_candidates תוך שמירה על המבנה המוצג
        self.assertEqual([item["chunk_id"] for item in select_top_k(candidates, 3)], ["B", "D", "A"])  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל

    def test_ties_and_short_lists(self):  # מגדירים מקרה בדיקה: ties and short lists
        candidates = [{"chunk_id": "B", "score": 0.8}, {"chunk_id": "A", "score": 0.8}]  # מכינים את מאגר הקטעים המועמדים לדירוג
        self.assertEqual([item["chunk_id"] for item in select_top_k(candidates, 8)], ["A", "B"])  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
        self.assertEqual(select_top_k([], 3), [])  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל

    def test_invalid_k_is_rejected(self):  # מגדירים מקרה בדיקה: invalid k is rejected
        for k in (0, -1, 1.5, True):  # עוברים לפי הסדר על k ומטפלים בכל פריט בנפרד
            with self.subTest(k=k), self.assertRaises(ValueError):  # פותחים משאב לזמן הפעולה וסוגרים אותו בסיומה
                select_top_k([], k)  # משלימים את ביטוי הנתונים של test_invalid_k_is_rejected תוך שמירה על המבנה המוצג

    def test_inputs_stay_unchanged(self):  # מגדירים מקרה בדיקה: inputs stay unchanged
        vector = [0.8, 0.6]  # שומרים את vector עבור המשך הפעולה
        cosine_similarity([1, 0], vector)  # משלימים את ביטוי הנתונים של test_inputs_stay_unchanged תוך שמירה על המבנה המוצג
        self.assertEqual(vector, [0.8, 0.6])  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
        candidates = [{"chunk_id": "B", "score": 0.8}, {"chunk_id": "A", "score": 0.8}]  # מכינים את מאגר הקטעים המועמדים לדירוג
        before = copy.deepcopy(candidates)  # שומרים עותק של הקלט כדי לבדוק שהפונקציה לא שינתה אותו
        select_top_k(candidates, 1)  # משלימים את ביטוי הנתונים של test_inputs_stay_unchanged תוך שמירה על המבנה המוצג
        self.assertEqual(candidates, before)  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל


if __name__ == "__main__":  # מפעילים את התוכנית כאשר מריצים את הקובץ עצמו
    unittest.main(verbosity=2)  # מריצים את כל בדיקות הקובץ ומציגים תוצאה לכל בדיקה
