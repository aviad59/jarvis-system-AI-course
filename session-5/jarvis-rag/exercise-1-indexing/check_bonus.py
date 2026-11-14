"""בדיקות לשני הבונוסים של תרגיל 1. מריצים: python check_bonus.py

אין כאן קריאה למודל אמיתי: קריאת ה־embeddings מוחלפת בפונקציה מקומית שסופרת קריאות.
"""
import argparse  # בונים אפשרויות הרצה כמו בשורת הפקודה
import io  # לוכדים את פלט ההדפסה של שלב embed
import json  # קוראים וכותבים קבצי index זמניים
import re  # מאתרים את שורות הטבלה בבדיקה
import tempfile  # תיקייה זמנית שאינה נוגעת ב־build
import unittest  # מסגרת הבדיקות המובנית
from contextlib import redirect_stdout  # לוכדים הדפסות בלי להציג אותן
from pathlib import Path  # נתיבים זמניים
from unittest.mock import patch  # מחליפים את קריאת המודל
import build_index  # קוד האינדוקס שהורחב בבונוס 2
import chunking  # החלוקה המבנית מבונוס 1
from index_support import BASE, load_documents, validate_pieces, digest  # טעינת מסמכים ובדיקת spans


def escalation_document(extra_rows):  # עותק של מסמך ESC-001 עם טבלה מוארכת
    document = dict(next(d for d in load_documents(BASE / "documents") if d["document_id"] == "ESC-001"))  # עותק בלבד
    lines = document["body"].split("\n")  # מאתרים את שורת P1 בטבלה
    index = next(i for i, line in enumerate(lines) if line.startswith("| P1 "))  # השורה האחרונה של הטבלה
    rows = [f"| P{3 + n} | תנאי בדיקה מספר {n} עם ערך סף {n}% במשך {n} דקות רצופות | צוות {n} בתוך {n} דקות |" for n in range(extra_rows)]  # שורות מידע נוספות
    document["body"] = "\n".join(lines[:index + 1] + rows + lines[index + 1:])  # הטבלה גדלה מעבר למגבלה
    return document  # המסמך המוארך


class StructuredChunking(unittest.TestCase):  # בונוס 1
    def test_corpus_spans_are_exact(self):  # כל קטע עדיין span מדויק ומכסה את הטקסט
        for document in load_documents(BASE / "documents"):  # כל המסמכים
            pieces = chunking.split_document_structured(document, 900)  # חלוקה מבנית
            validate_pieces(document, pieces, 900)  # אותו חוזה כמו בליבה
            for piece in pieces:  # שני הייצוגים
                self.assertEqual(piece["source_text"], document["body"][piece["start"]:piece["end"]])  # source_text עקיב למקור
                self.assertTrue(piece["embedding_text"].endswith(piece["text"]))  # ההקשר רק מתווסף לפני הטקסט

    def test_heading_stays_with_content(self):  # אין קטע שמסתיים בכותרת יתומה
        for document in load_documents(BASE / "documents"):  # כל המסמכים
            for piece in chunking.split_document_structured(document, 300):  # מגבלה קטנה מכריחה הרבה חלוקות
                self.assertFalse(piece["text"].rstrip().split("\n")[-1].startswith("#"), piece["text"])  # כותרת אינה השורה האחרונה

    def test_long_table_split_between_rows_with_headers(self):  # טבלת ההסלמה המוארכת
        document = escalation_document(25)  # טבלה ארוכה מ־900 תווים
        pieces = chunking.split_document_structured(document, 900)  # חלוקה מבנית
        validate_pieces(document, pieces, 900)  # כל טקסט נשמר ועקיב למקור
        body = document["body"]  # הטקסט המקורי
        data_rows = [m for m in re.finditer(r"^\| P\d+ .*\|$", body, re.M)]  # כל שורות המידע בטבלה
        table_pieces = [p for p in pieces if "| P" in p["text"]]  # הקטעים שמכילים חלקי טבלה
        self.assertGreater(len(table_pieces), 1)  # הטבלה אכן חולקה
        header = "| סיווג ראשוני | תנאי | מי מעורב ומתי |"  # כותרות העמודות
        for piece in table_pieces:  # בכל חלק
            self.assertIn(header, piece["embedding_text"])  # הכותרות זמינות למודל
            self.assertIn("## ספי התחברות", piece["embedding_text"])  # וגם כותרת הסעיף
        for row in data_rows:  # כל שורת מידע
            owners = [p for p in table_pieces if p["start"] <= row.start() and row.end() <= p["end"]]  # הקטע שמכיל את השורה כולה
            self.assertEqual(len(owners), 1, row.group())  # השורה שלמה בקטע אחד בדיוק — לא נחתכה
            self.assertEqual(body[row.start():row.end()], row.group())  # ואפשר לחזור אליה במקור לפי המיקום


class IncrementalIndex(unittest.TestCase):  # בונוס 2
    def setUp(self):  # תיקייה זמנית עם עותק של המסמכים
        self.tmp = tempfile.TemporaryDirectory()  # נמחקת בסוף הבדיקה
        self.dir = Path(self.tmp.name)  # נתיב התיקייה
        (self.dir / "docs").mkdir()  # עותק המסמכים
        for path in (BASE / "documents").glob("*.md"):  # מעתיקים את כל המסמכים
            (self.dir / "docs" / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")  # תוכן זהה
        self.calls = []  # כמה טקסטים נשלחו "למודל"

    def tearDown(self):  # ניקוי
        self.tmp.cleanup()  # מוחקים את התיקייה הזמנית

    def fake_embed(self, base_url, model, texts):  # מחליף את Ollama: וקטור דטרמיניסטי לכל טקסט
        self.calls.extend(texts)  # סופרים כל טקסט שחושב
        return [[b / 255 for b in bytes.fromhex(digest(model + t))[:8]] for t in texts]  # הווקטור תלוי במודל ובקלט

    def build(self, model="embeddinggemma", digest_value="sha-A", reuse=True):  # prepare + embed עם counters
        chunks, index = self.dir / "chunks.json", self.dir / "index.json"  # קבצי ביניים
        prepare_args = argparse.Namespace(documents=str(self.dir / "docs"), implementation=str(BASE / "chunking.py"),  # כמו prepare בשורת הפקודה
                                          max_chars=900, output=str(chunks), structured=False)  # חלוקת הליבה
        embed_args = argparse.Namespace(input=str(chunks), output=str(index), provider="ollama", model=model,  # כמו embed בשורת הפקודה
                                        base_url="http://unused", document_format=None, fixture=False, ask_key=False,  # אין רשת
                                        reuse=str(index) if reuse else None, no_atlas=True, atlas_env=None)  # מצביעים על ה־index הקודם
        self.calls = []  # מאפסים ספירה
        output = io.StringIO()  # לוכדים הדפסות
        with redirect_stdout(output), patch.object(build_index, "embed_texts", self.fake_embed), \
                patch.object(build_index, "resolve_model", lambda url, m: (m + ":latest", digest_value)):  # מודל ו־digest מדומים
            build_index.prepare(prepare_args)  # חלוקה
            build_index.embed(embed_args)  # embeddings עם שימוש חוזר
        line = next(l for l in output.getvalue().splitlines() if l.startswith("REUSE_COUNTERS"))  # שורת ה־counters
        counters = dict(re.findall(r"(\w+)=(\d+)", line))  # מפרקים למילון
        return {k: int(v) for k, v in counters.items()}, json.loads(index.read_text(encoding="utf-8"))  # counters + index

    def test_three_runs_and_model_change(self):  # ללא שינוי; שינוי פסקה; מחיקת מסמך; שינוי מודל
        first, index = self.build()  # הרצה ראשונה: אין index קודם
        total = index["manifest"]["chunk_count"]  # מספר הקטעים
        self.assertEqual((first["reused"], first["embedded"]), (0, total))  # הכול חושב

        unchanged, _ = self.build()  # 1) ללא שינוי
        self.assertEqual((unchanged["reused"], unchanged["embedded"], unchanged["removed"]), (total, 0, 0))  # שום חישוב
        self.assertEqual(self.calls, [])  # המודל לא נקרא בכלל

        path = self.dir / "docs" / "04-notification-runbook.md"  # 2) שינוי פסקה אחת
        text = path.read_text(encoding="utf-8")  # התוכן הנוכחי
        self.assertIn("משתמשים רואים", text)  # הפסקה שמשנים נמצאת בגוף המסמך
        path.write_text(text.replace("משתמשים רואים", "משתמשים מדווחים שהם רואים", 1), encoding="utf-8")  # שינוי קטן בפסקה אחת
        changed, _ = self.build()  # הרצה אחרי שינוי
        self.assertEqual(changed["embedded"], 1)  # רק הקטע ששונה חושב מחדש
        self.assertEqual(changed["removed"], 1)  # הווקטור הישן של אותו קטע הוסר
        self.assertEqual(changed["reused"], total - 1)  # כל השאר נשמרו

        before, _ = self.build()  # מצב יציב לפני מחיקה
        (self.dir / "docs" / "06-incident-notifications-october.md").unlink()  # 3) מחיקת מסמך
        deleted, index = self.build()  # הרצה אחרי מחיקה
        self.assertEqual(deleted["embedded"], 0)  # אין מה לחשב
        self.assertGreaterEqual(deleted["removed"], 1)  # הקטעים של המסמך הוסרו
        self.assertFalse(any(r["metadata"]["source"].endswith("06-incident-notifications-october.md") for r in index["records"]))  # ואינם ב־index

        new_digest, index = self.build(digest_value="sha-B")  # 4) אותו שם מודל, משקולות אחרות
        self.assertEqual((new_digest["reused"], new_digest["embedded"]), (0, index["manifest"]["chunk_count"]))  # הכול חושב מחדש

    def test_inserted_paragraph_shifts_offsets_without_recompute(self):  # פסקה חדשה מזיזה מיקומים
        self.build()  # index ראשון
        path = self.dir / "docs" / "01-service-overview.md"  # מסמך עם כמה קטעים
        text = path.read_text(encoding="utf-8")  # תוכן נוכחי
        head, meta, body = text.split("\n\n", 2)  # כותרת, metadata, גוף
        path.write_text(head + "\n\n" + meta + "\n\n" + "פסקה חדשה בראש המסמך.\n\n" + body, encoding="utf-8")  # מוסיפים פסקה בתחילת הגוף
        counters, index = self.build()  # הרצה אחרי ההוספה
        self.assertGreaterEqual(counters["moved_or_metadata_updated"], 1)  # קטעים שלא השתנו קיבלו מיקום חדש
        for record in index["records"]:  # כל קטע עדיין מצביע למקום הנכון
            if record["metadata"]["source"].endswith("01-service-overview.md"):  # רק המסמך ששונה
                body_now = path.read_text(encoding="utf-8").split("\n\n", 2)[2]  # הגוף המעודכן
                self.assertEqual(body_now[record["start"]:record["end"]], record["text"])  # המיקום עודכן ולא נשאר ישן


if __name__ == "__main__":  # הרצה ישירה
    unittest.main(verbosity=2)  # מציגים תוצאה לכל בדיקה
