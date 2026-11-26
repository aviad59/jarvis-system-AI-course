# נקודת ההפעלה: לאחר השלמת student.py מריצים עם תוצאות החיפוש מתרגיל2.
import argparse  # קוראים אפשרויות משורת הפקודה.
import json  # שומרים תשובה והקשר בפורמט JSON.
from pathlib import Path  # עובדים עם קבצים ותיקיות בכל מערכות ההפעלה.
from runtime import generate, load_search, write_view  # מייבאים את תשתית החיבור והצגת הפלט המסופקת.
from student import assemble_context, invalid_citation_ids  # מייבאים את הפונקציות שהתלמידים משלימים.

ENDPOINTS = {"anthropic": "https://api.anthropic.com/v1", "gemini": "https://generativelanguage.googleapis.com/v1beta"}  # כתובות ברירת המחדל של ספקי הענן.

def main():  # מרכזים את סדר ההרצה בפונקציה אחת.
    parser = argparse.ArgumentParser(description="Build context and generate an incident answer")  # יוצרים עזרה שמוצגת עם האפשרות help.
    parser.add_argument("--input", default="fixtures/search-results.json")  # ברירת המחדל היא קלט סינתטי מסומן; להמשך אמיתי נותנים נתיב לפלט תרגיל2.
    parser.add_argument("--provider", choices=["ollama", "anthropic", "gemini", "offline_fixture"], default="offline_fixture")  # ברירת המחדל פועלת ללא מודל וללא תשלום.
    parser.add_argument("--model")  # במסלול אמיתי בוחרים מודל זמין בחשבון או בשרת המקומי.
    parser.add_argument("--endpoint")  # אפשר לציין כתובת שרת אחרת במפורש.
    parser.add_argument("--max-chars", type=int, default=12000)  # מגבילים את טקסט המקורות בתווים; זו אינה ספירת טוקנים.
    parser.add_argument("--timeout", type=int, default=120)  # מגבילים את משך בקשת HTTP אחת בשניות.
    parser.add_argument("--fixture", default="fixtures/answer.json")  # בוחרים תשובה מוכנה למסלול offline_fixture בלבד.
    parser.add_argument("--output", default="artifacts/answer.json")  # שומרים כל ניסיון בשם נפרד אם רוצים להשוות בין הרצות.
    parser.add_argument("--history", help="JSON file with the earlier turns [{question, answer}] (Jarvis conversation)")
    parser.add_argument("--require-citations", action="store_true")  # בונוס1: דורשים שהבודק האוטומטי של ההפניות כבר יהיה ממומש.
    args = parser.parse_args()  # מפענחים את האפשרויות שהוקלדו בטרמינל.
    try:  # מטפלים בשגיאות צפויות כדי לתת הודעה שאפשר לפעול לפיה.
        if args.max_chars < 1 or args.timeout < 1:  # בודקים שהמגבלות הן מספרים חיוביים.
            raise ValueError("max-chars and timeout must be positive")  # עוצרים לפני בקשה בעלת מגבלה לא תקינה.
        if args.provider != "offline_fixture" and not args.model:  # במסלול אמיתי אין להניח שדגם כלשהו כבר מותקן או נגיש.
            raise ValueError("Select an available generation model with --model")  # מסבירים איך לבחור Generation Model; זה אינו Embedding Model.
        search = load_search(args.input)  # טוענים את השאלה והמקורות שהוחזרו בחיפוש.
        context = assemble_context(search["question"], search["results"], args.max_chars)
        if args.history:  # earlier turns: only to understand what the question refers to, not evidence
            turns = json.loads(Path(args.history).read_text(encoding="utf-8"))
            if turns:
                context["conversation"] = turns  # מרכיבים את ההקשר באמצעות הקוד שהתלמידים משלימים.
        endpoint = args.endpoint or ENDPOINTS.get(args.provider, "http://localhost:11434")  # בוחרים את כתובת הספק; הנתיבים של Messages, של Gemini ושל Ollama שונים.
        answer, seconds = generate(context, args.provider, args.model, endpoint, args.timeout, args.fixture)  # מקבלים תשובה ממודל או תשובה קבועה מה־fixture המסומן.
        try:  # בודק ההפניות הוא בונוס נפרד מליבת התרגיל.
            invalid = invalid_citation_ids(answer, context["sources"])  # משווים את ההפניות למקורות שנשלחו בפועל, לא לכל האינדקס.
            citation_status = "invalid_source_ids" if invalid else "source_ids_valid_not_semantic_proof"  # שם הסטטוס מזכיר שהתאמת מזהה אינה הוכחת תמיכה בטענה.
        except NotImplementedError:  # אם הבונוס טרם מומש, אפשר להמשיך את ליבת התרגיל.
            if args.require_citations:  # כאשר ביקשו את הבדיקה במפורש, אין לדלג על מימוש חסר.
                raise  # מעבירים את הודעת ה־TODO אל מטפל השגיאות הראשי.
            invalid, citation_status = [], "citations_not_validated"  # מסמנים שהבדיקה לא בוצעה; אין כאן אישור להפניות.
        report = {"schema_version": 1, "provider": args.provider, "model": args.model, "retrieval_mode": search["mode"], "index_manifest": search["index_manifest"], "seconds": seconds, "context": context, "answer": answer, "citation_status": citation_status, "invalid_source_ids": invalid}  # שומרים את מקור החיפוש, ההקשר והתשובה יחד כדי לאפשר אבחון.
        output = Path(args.output)  # ממירים את שם קובץ הפלט לאובייקט נתיב.
        output.parent.mkdir(parents=True, exist_ok=True)  # יוצרים את תיקיית התוצאות אם היא עדיין אינה קיימת.
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")  # שומרים JSON קריא הכולל את טקסט המקורות שנשלחו.
        write_view(report, output.with_suffix(".html"))  # יוצרים תצוגה שמשווה כל טענה למקורות המצוטטים שלה.
        print("provider=" + args.provider + " citation_status=" + citation_status)  # משתמשים בתוויות אנגליות כדי למנוע היפוך בטרמינל.
        print("sources=" + str(len(context["sources"])) + " seconds=" + str(seconds))  # זמן fixture אינו זמן תגובה של LLM ואינו מתאים להשוואת ביצועים.
        print("saved=" + str(output) + " viewer=" + str(output.with_suffix(".html")))  # מציגים את הנתיבים המדויקים שנוצרו.
        return 2 if invalid else 0  # שומרים דו״ח גם עם הפניות שגויות, אך מחזירים קוד כישלון.
    except (ValueError, OSError, RuntimeError, NotImplementedError, KeyError, TypeError) as error:  # תופסים שגיאות קלט, תקשורת, JSON ו־TODO שעדיין לא הושלם.
        print("ERROR: " + str(error))  # בהודעות השגיאה שהתשתית יוצרת אין הדפסה של מפתח API.
        return 1  # קוד1 מציין שההרצה נעצרה לפני תוצאה מלאה.

if __name__ == "__main__":  # מפעילים את main רק בהרצת הקובץ ישירות.
    raise SystemExit(main())  # מעבירים את קוד ההצלחה או הכישלון למערכת ההפעלה.
