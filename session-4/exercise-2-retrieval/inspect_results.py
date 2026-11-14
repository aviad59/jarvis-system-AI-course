"""מציג את השאלה, הדירוג, הטקסט המלא ומקור כל קטע שנבחר.

הציונים הם Cosine Similarity. בדיקת התוכן והמקור היא שלב נפרד
מהמיון המספרי, ובלעדיה אי אפשר לדעת אם חזר מידע מספיק לתשובה.
"""
import argparse  # טוענים כלי עבור קריאת אפשרויות משורת הפקודה
import html  # טוענים כלי עבור הצגת טקסט המקור בבטחה בתוך HTML
import json  # טוענים כלי עבור קריאה וכתיבה של קבצי התוצרים
from pathlib import Path  # משתמשים בנתיבים שמאפשרים לפתוח ולשמור את קבצי התרגיל


def main():  # מגדירים אפשרויות הרצה ומפעילים את המסלול שהלומד בחר
    parser = argparse.ArgumentParser()  # מכינים את קורא אפשרויות ההרצה
    parser.add_argument("path", nargs="?", default="build/search-results.json")  # מגדירים אפשרות הרצה: path
    parser.add_argument("--html", help="Write a local readable results page")  # מגדירים אפשרות הרצה: --html
    args = parser.parse_args()  # קוראים את אפשרויות ההרצה שנבחרו
    data = json.loads(Path(args.path).read_text(encoding="utf-8"))  # קוראים את נתוני התוצר או תשובת ה־API
    print("MODE:", data["mode"])  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print("QUESTION:", data["question"])  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    cards = []  # אוספים תיבות HTML להצגת כל הקטעים
    for result in data["results"]:  # עוברים לפי הסדר על result ומטפלים בכל פריט בנפרד
        metadata = result["metadata"]  # שומרים שדות המאפשרים לזהות מקור וגרסה
        print(f"\nRANK {result['rank']} | COSINE {result['score']:.6f}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        print("SOURCE:", metadata["source"])  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        print("VERSION:", metadata["version"], "STATUS:", metadata["status"])  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        print("FULL CHUNK TEXT:\n" + result["text"])  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        cards.append(f'<section><h2>Rank {result["rank"]}</h2><p dir="ltr">Cosine Similarity: {result["score"]:.6f}</p>'  # מוסיפים את התוצאה לרשימה תוך שמירה על סדר העיבוד
                     f'<pre>{html.escape(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre>'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                     f'<div class="text">{html.escape(result["text"])}</div></section>')  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
    if args.html:  # יוצרים גם תצוגת דפדפן רק אם נבחר קובץ HTML לפלט
        page = ('<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">'  # בונים עמוד HTML מקומי לעיון בתוצר
                '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Retrieval results</title>'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                '<style>body{font:18px Arial;line-height:1.75;max-width:1000px;margin:auto;padding:26px;background:#eef2f5;color:#18313d}'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                'section{background:white;padding:24px;border-radius:12px;margin:20px 0}pre{direction:ltr;text-align:left;'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                'white-space:pre-wrap;overflow-wrap:anywhere}.text{white-space:pre-wrap}</style>'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                f'<h1>מה החיפוש החזיר?</h1><p>{html.escape(data["question"])}</p>'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                f'<p dir="ltr">Mode: {html.escape(data["mode"])}</p>'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                '<p>הציון מסדר התאמה בין וקטורים. בודקים בנפרד אם הקטעים תומכים בתשובה ואם הם מהגרסה המתאימה.</p>'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                + ''.join(cards))  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
        path = Path(args.html)  # שומרים את הנתיב לקובץ שהפעולה הנוכחית קוראת או כותבת
        path.parent.mkdir(parents=True, exist_ok=True)  # מכינים תיקיית תוצרים אם היא עדיין אינה קיימת
        path.write_text(page, encoding="utf-8")  # שומרים את התוצר בקובץ כדי שאפשר יהיה לפתוח ולהעביר אותו לשלב הבא
        print("HTML_VIEWER:", path.resolve())  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה


if __name__ == "__main__":  # מפעילים את התוכנית כאשר מריצים את הקובץ עצמו
    main()  # משלימים את ביטוי הנתונים של inspect_results תוך שמירה על המבנה המוצג
