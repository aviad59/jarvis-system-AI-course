# הבדיקות מפעילות קוד מקומי ו־HTTP mock בלבד; הן אינן פונות לספק.
import argparse  # בוחרים אם לבדוק את הליבה או את הבונוס.
import copy  # משנים עותק של הקלט כדי לבדוק מקרי קצה.
import json  # קוראים את ה־fixture שמגיע בחבילה.
import os  # נותנים מפתח דמה לבדיקת מעטפת Anthropic.
import tempfile  # יוצרים קבצים זמניים לבדיקות שגיאה ותצוגה.
import unittest  # מפעילים בדיקות אוטומטיות מהספרייה המובנית.
from pathlib import Path  # מאתרים את הקלט ביחס לקובץ הבדיקות.
from unittest.mock import patch  # מחליפים HTTP בפונקציה מקומית בלי רשת.
import runtime  # בודקים את התשתית ואת ההגנות שלה.
import student  # בודקים את הפונקציות שהתלמידים משלימים.

DATA = json.loads((Path(__file__).parent / "fixtures/search-results.json").read_text(encoding="utf-8"))  # קלט קבוע מאפשר לשחזר את הבדיקה.
ANSWER = json.loads((Path(__file__).parent / "fixtures/answer.json").read_text(encoding="utf-8"))  # זוהי תשובה מוכנה, לא פלט של הרצת LLM בבדיקה.

class Core(unittest.TestCase):  # בדיקות הליבה בודקות הרכבת הקשר והתנהגות שגיאות.
    def test_keep_question_and_sources(self):  # בודקים שהשאלה והמקור נשמרים בלי המצאה.
        result = student.assemble_context(DATA["question"], DATA["results"], 20000)  # נותנים תקציב שמספיק לכל הקטעים.
        self.assertEqual(result["question"], DATA["question"])  # השאלה המקורית אינה מוחלפת או מקוצרת.
        self.assertEqual(result["sources"][0]["text"], DATA["results"][0]["text"])  # טקסט המקור נשמר בדיוק.
        self.assertEqual(result["sources"][0]["metadata"], DATA["results"][0]["metadata"])  # נשמר גם הייחוס למקור.
    def test_budget_whole_chunks(self):  # בודקים את גבול התקציב בלי לחתוך קטע באמצע.
        budget = len(DATA["results"][0]["text"])  # התקציב מכיל בדיוק את הקטע הראשון.
        result = student.assemble_context(DATA["question"], DATA["results"], budget)  # השני לא ייכנס בתקציב שנותר.
        self.assertEqual(len(result["sources"]), 1)  # נשמר רק מקור שלם אחד.
        self.assertEqual(result["sources"][0]["text"], DATA["results"][0]["text"])  # לא מקבלים חצי משפט במקום ראיה.
    def test_rank_and_duplicates(self):  # בודקים בחירה לפי rank וגם מניעת כפילויות.
        rows = list(reversed(DATA["results"])) + [DATA["results"][0]]  # הקלט מכיל סדר הפוך וכפילות מכוונת.
        result = student.assemble_context(DATA["question"], rows, 20000)  # בפונקציית הלמידה מטפלים גם במקרה זה.
        self.assertEqual([row["chunk_id"] for row in result["sources"]], [row["chunk_id"] for row in DATA["results"]])  # הפלט ייחודי ומסודר.
    def test_no_evidence_fits(self):  # תקציב קטן מדי מחייב הודעה לפני פנייה למודל.
        with self.assertRaises(ValueError):  # מצפים לעצירה מכוונת עם הסבר.
            student.assemble_context(DATA["question"], DATA["results"], 1)  # שום מקור שלם אינו נכנס בתו אחד.
    def test_missing_citation(self):  # גם לפני הבונוס נדרשת הפניה בטקסט המובנה.
        answer = copy.deepcopy(ANSWER)  # משאירים את ה־fixture המקורי ללא שינוי.
        answer["claims"][0]["citation_ids"] = []  # יוצרים טענה ללא מקור.
        with self.assertRaises(ValueError):  # מוודאים שאין הצגת טענה זו כתשובה תקינה.
            runtime.validate_answer(answer)  # זו בדיקת מבנה, לא בדיקת תמיכה.
    def test_empty_results(self):  # תוצאות חיפוש ריקות דורשות חזרה לשלבRetrieval.
        data = copy.deepcopy(DATA)  # עורכים עותק כדי לא להשפיע על בדיקות אחרות.
        data["results"] = []  # מדמים חיפוש שלא החזיר מקורות.
        with tempfile.TemporaryDirectory() as directory:  # שומרים קלט שגוי בתיקייה זמנית.
            path = Path(directory) / "empty.json"  # מגדירים את מיקום הקלט לבדיקה.
            path.write_text(json.dumps(data), encoding="utf-8")  # כותבים את הקלט בדיוק כמו קובץ שלב2.
            with self.assertRaises(ValueError):  # אין קריאת מודל כאשר אין ראיות.
                runtime.load_search(path)  # טוען הקלט אמור לעצור כאן.
    def test_anthropic_mock(self):  # בודקים את מבנה Messages ללא גישה לחשבון אמיתי.
        response = {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": json.dumps(ANSWER)}]}  # תשובת HTTP מדומה.
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-only"}), patch("runtime.post_json", return_value=response) as post:  # אין קריאה לרשת ואין קריאה של מפתח אמיתי.
            answer, seconds = runtime.generate({"question": "q", "sources": []}, "anthropic", "test-model", "https://example.invalid/v1", 1, "unused")  # מפעילים רק את מסלול הפענוח.
            self.assertEqual(answer, ANSWER)  # בודקים שנשלף טקסט JSON מתוך מקטע text בלבד.
            self.assertEqual(post.call_args.args[0], "https://example.invalid/v1/messages")  # הנתיב מתאים ל־Messages API.
            self.assertEqual(post.call_args.args[2]["x-api-key"], "fake-only")  # המפתח נשלח בכותרת x-api-key.
    def test_anthropic_refusal(self):  # סירוב אינו מוצג כתשובה.
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-only"}), patch("runtime.post_json", return_value={"stop_reason": "refusal", "content": []}):  # תשובת סירוב מדומה.
            with self.assertRaises(ValueError):  # מצפים לעצירה מפורשת.
                runtime.generate({"question": "q", "sources": []}, "anthropic", "test-model", "https://example.invalid/v1", 1, "unused")  # אין רשת.
    def test_gemini_mock(self):  # בודקים את מבנה generateContent ללא גישה לחשבון אמיתי.
        response = {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": "thinking", "thought": True}, {"text": json.dumps(ANSWER)}]}}]}  # תשובת HTTP מדומה.
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-only"}), patch("runtime.post_json", return_value=response) as post:  # אין קריאה לרשת ואין קריאה של מפתח אמיתי.
            answer, seconds = runtime.generate({"question": "q", "sources": []}, "gemini", "test-model", "https://example.invalid/v1beta", 1, "unused")  # מפעילים רק את מסלול הפענוח.
            self.assertEqual(answer, ANSWER)  # חלקי thought אינם חלק מהתשובה.
            self.assertEqual(post.call_args.args[0], "https://example.invalid/v1beta/models/test-model:generateContent")  # הנתיב מתאים ל־generateContent.
            self.assertEqual(post.call_args.args[2], {"x-goog-api-key": "fake-only"})  # המפתח נשלח בכותרת, לא בכתובת.
            config = post.call_args.args[1]["generationConfig"]  # Structured Output לפי אותה סכמה.
            self.assertEqual((config["responseMimeType"], config["responseJsonSchema"]), ("application/json", runtime.SCHEMA))
            self.assertIn("systemInstruction", post.call_args.args[1])  # ההנחיות נשלחות בנפרד מהמקורות.
    def test_gemini_blocked_or_cut(self):  # חסימה, סירוב או פלט שנקטע אינם מוצגים כתשובה.
        for response in ({"promptFeedback": {"blockReason": "SAFETY"}}, {"candidates": [{"finishReason": "SAFETY"}]}, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{"}]}}]}):  # שלוש תשובות מדומות.
            with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-only"}), patch("runtime.post_json", return_value=response):
                with self.assertRaises(ValueError):  # מצפים לעצירה מפורשת.
                    runtime.generate({"question": "q", "sources": []}, "gemini", "test-model", "https://example.invalid/v1beta", 1, "unused")
    def test_gemini_needs_key(self):  # אין פנייה לענן בלי מפתח.
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}), patch("runtime.post_json") as post:
            with self.assertRaises(ValueError):
                runtime.generate({}, "gemini", "test-model", "https://example.invalid/v1beta", 1, "unused")
            post.assert_not_called()
    def test_ollama_mock(self):  # בודקים את מבנה Native Ollama ללא שרת מקומי.
        with patch("runtime.post_json", return_value={"done": True, "message": {"content": json.dumps(ANSWER)}}) as post:  # מחליפים את כל פעולת ה־HTTP.
            answer, seconds = runtime.generate({}, "ollama", "test-model", "http://example.invalid", 1, "unused")  # התשובה מגיעה מה־mock בלבד.
            self.assertEqual(answer, ANSWER)  # בודקים את פענוח תוכן המענה.
            self.assertTrue(post.call_args.args[0].endswith("/api/chat"))  # הנתיב שונה מנתיב Anthropic.
    def test_invalid_json(self):  # תשובה לא תקינה חייבת להיעצר לפני דו״ח הצלחה.
        with patch("runtime.post_json", return_value={"done": True, "message": {"content": "not-json"}}):  # מכינים פלט פגום במכוון.
            with self.assertRaises(ValueError):  # JSONDecodeError יורשת מ־ValueError.
                runtime.generate({}, "ollama", "test-model", "http://example.invalid", 1, "unused")  # אין רשת; נבדק פענוח בלבד.
    def test_html_escapes_text(self):  # טקסט מקור או מודל לא אמור להפוך לקוד HTML פעיל.
        report = {"context": {"sources": []}, "answer": {"claims": [{"text": "<script>alert(1)</script>", "citation_ids": ["missing"]}], "missing_information": []}, "provider": "offline_fixture", "citation_status": "citations_not_validated", "seconds": 0}  # קלט מכוון עם תגים.
        with tempfile.TemporaryDirectory() as directory:  # בודקים קובץ תצוגה זמני.
            path = Path(directory) / "view.html"  # נתיב הדף שייווצר.
            runtime.write_view(report, path)  # מפעילים את יוצר התצוגה.
            text = path.read_text(encoding="utf-8")  # קוראים את הדף לבדיקה.
            self.assertNotIn("<script>", text)  # התגים אינם נשמרים כתגי קוד פעילים.
            self.assertIn("SOURCE NOT SENT TO MODEL", text)  # מקור חסר מסומן באופן מפורש.

class Bonus(unittest.TestCase):  # בדיקות שמפעילים לאחר מימוש בונוס1.
    def test_unknown_and_not_sent(self):  # מקור קיים שלא נשלח אינו מקור מותר לציטוט.
        context = student.assemble_context(DATA["question"], DATA["results"], len(DATA["results"][0]["text"]))  # שולחים רק מקור ראשון.
        answer = copy.deepcopy(ANSWER)  # שומרים את התשובה המקורית כדי להריץ שוב.
        answer["claims"][0]["citation_ids"] = ["unknown", DATA["results"][1]["chunk_id"], "unknown"]  # בודקים גם כפילות של מזהה שגוי.
        self.assertEqual(student.invalid_citation_ids(answer, context["sources"]), sorted(["unknown", DATA["results"][1]["chunk_id"]]))  # מצפים לשגויים ייחודיים בסדר קבוע.
    def test_valid_ids(self):  # התאמת מזהים צריכה לעבור בלי להבטיח אמיתות של הטענה.
        context = student.assemble_context(DATA["question"], DATA["results"], 20000)  # שני המקורות כלולים בבקשה.
        self.assertEqual(student.invalid_citation_ids(ANSWER, context["sources"]), [])  # אין מזהים לא מוכרים בתשובת הדוגמה.

if __name__ == "__main__":  # יבוא הקובץ לא מריץ את הבדיקות אוטומטית.
    parser = argparse.ArgumentParser()  # יוצרים בורר קבוצת בדיקות.
    parser.add_argument("--bonus", action="store_true")  # האפשרות מוסיפה את בדיקות הבונוס לליבה.
    args = parser.parse_args()  # קוראים את הבורר מהטרמינל.
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Core)  # מתחילים מעשר בדיקות הליבה.
    if args.bonus:  # בדיקות הבונוס רצות רק לפי בקשה מפורשת.
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(Bonus))  # מוסיפים את שתי הבדיקות של מזהי המקורות.
    result = unittest.TextTestRunner(verbosity=2).run(suite)  # מציגים שמות בדיקות כדי שניתן יהיה לזהות כשל.
    raise SystemExit(0 if result.wasSuccessful() else 1)  # קוד התהליך מציין אם כל הבדיקות עברו.
