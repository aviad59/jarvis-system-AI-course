"""בדיקות לחוזה של שתי הפונקציות שבונים בתרגיל.

מריצים python check_exercise.py. בתחילת התרגיל מופיעות שגיאות TODO מכוונות.
אין כאן קריאת מודל. הבדיקות בוחנות שמירת טקסט, מקור וגרסה.
"""
import unittest  # טוענים כלי עבור בדיקת חוזי הפונקציות ללא מודל
import chunking  # טוענים כלי עבור תמיכה בבדיקת התרגיל
from index_support import BASE, REQUIRED, load_documents, validate_pieces  # טוענים את פונקציות העזר הדרושות לשלב הנוכחי


class ChunkingChecks(unittest.TestCase):  # מקבצים בדיקות של אותו חוזה התנהגות כדי להריצן יחד
    def test_corpus_text_is_preserved(self):  # מגדירים מקרה בדיקה: corpus text is preserved
        # כל תו בעל תוכן חייב להופיע בקטע כלשהו; השמטת תנאי משנה תשובה.
        for document in load_documents(BASE / "documents"):  # עוברים לפי הסדר על document ומטפלים בכל פריט בנפרד
            with self.subTest(source=document["source"]):  # פותחים משאב לזמן הפעולה וסוגרים אותו בסיומה
                pieces = chunking.split_document(document, 900)  # מכינים את הקטעים שהחלוקה החזירה עבור מסמך אחד
                validate_pieces(document, pieces, 900)  # משלימים את ביטוי הנתונים של test_corpus_text_is_preserved תוך שמירה על המבנה המוצג

    def test_long_paragraph_has_bounded_chunks(self):  # מגדירים מקרה בדיקה: long paragraph has bounded chunks
        # מקרה קצה קטן מוודא שפסקה ארוכה לא עוברת בלי חלוקה נוספת.
        document = {"body": "condition important " * 120, "source": "test.md"}  # שומרים את document עבור המשך הפעולה
        pieces = chunking.split_document(document, 200)  # מכינים את הקטעים שהחלוקה החזירה עבור מסמך אחד
        validate_pieces(document, pieces, 200)  # משלימים את ביטוי הנתונים של test_long_paragraph_has_bounded_chunks תוך שמירה על המבנה המוצג
        self.assertGreater(len(pieces), 1)  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל

    def test_metadata_points_back_to_exact_source_version(self):  # מגדירים מקרה בדיקה: metadata points back to exact source version
        # שתי גרסאות בעלות אותו Document ID צריכות להישאר נבדלות.
        documents = load_documents(BASE / "documents")  # טוענים את מסמכי המקור לתרגול
        versions = set()  # שומרים את versions עבור המשך הפעולה
        for document in documents:  # עוברים לפי הסדר על document ומטפלים בכל פריט בנפרד
            piece = {"text": document["body"][:50], "start": 0, "end": 50}  # שומרים את piece עבור המשך הפעולה
            metadata = chunking.make_metadata(document, piece)  # שומרים שדות המאפשרים לזהות מקור וגרסה
            for field in REQUIRED:  # עוברים לפי הסדר על field ומטפלים בכל פריט בנפרד
                self.assertEqual(metadata[field], document[field])  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל
            if document["document_id"] == "RB-AUTH":  # בודקים את תנאי הקלט שעליו נשען הענף הבא
                versions.add((metadata["version"], metadata["status"]))  # מעדכנים את קבוצת הערכים שנאספו לצורך בדיקה
        self.assertEqual(versions, {("1.0", "superseded"), ("2.0", "current")})  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל

    def test_input_document_stays_unchanged(self):  # מגדירים מקרה בדיקה: input document stays unchanged
        # תהליך האינדוקס אמור לייצר רשומות חדשות ולשמור את המקור לעיון.
        document = load_documents(BASE / "documents")[0]  # שומרים את document עבור המשך הפעולה
        before = dict(document)  # שומרים עותק של הקלט כדי לבדוק שהפונקציה לא שינתה אותו
        pieces = chunking.split_document(document, 900)  # מכינים את הקטעים שהחלוקה החזירה עבור מסמך אחד
        chunking.make_metadata(document, pieces[0])  # משלימים את ביטוי הנתונים של test_input_document_stays_unchanged תוך שמירה על המבנה המוצג
        self.assertEqual(document, before)  # בודקים שהערך שהתקבל עומד בחוזה הצפוי של התרגיל


if __name__ == "__main__":  # מפעילים את התוכנית כאשר מריצים את הקובץ עצמו
    unittest.main(verbosity=2)  # מריצים את כל בדיקות הקובץ ומציגים תוצאה לכל בדיקה
