"""מציג chunk, טקסט מקור, metadata והווקטור שנשמר לידם.

אפשר לפתוח את אותו תוצר גם בתצוגת HTML, שבה העברית מסודרת מימין לשמאל.
הקובץ קורא קבצים מקומיים בלבד; הוא אינו מפעיל מודל.
"""
import argparse  # טוענים כלי עבור קריאת אפשרויות משורת הפקודה
import html  # טוענים כלי עבור הצגת טקסט המקור בבטחה בתוך HTML
import json  # טוענים כלי עבור קריאה וכתיבה של קבצי התוצרים
from pathlib import Path  # משתמשים בנתיבים שמאפשרים לפתוח ולשמור את קבצי התרגיל


def main():  # מגדירים אפשרויות הרצה ומפעילים את המסלול שהלומד בחר
    parser = argparse.ArgumentParser(description="Inspect one chunk or create a readable HTML index viewer")  # מכינים את קורא אפשרויות ההרצה
    parser.add_argument("path", help="Prepared chunks JSON or embedded index JSON")  # מגדירים אפשרות הרצה: path
    parser.add_argument("--record", type=int, default=1, help="1-based record number")  # מגדירים אפשרות הרצה: --record
    parser.add_argument("--full-vector", action="store_true")  # מגדירים אפשרות הרצה: --full-vector
    parser.add_argument("--html", help="Write a local RTL HTML viewer containing all records")  # מגדירים אפשרות הרצה: --html
    args = parser.parse_args()  # קוראים את אפשרויות ההרצה שנבחרו
    data = json.loads(Path(args.path).read_text(encoding="utf-8"))  # קוראים את נתוני התוצר או תשובת ה־API
    records = data["records"]  # שומרים את רשומות הקטעים לשלב הבא
    if not 1 <= args.record <= len(records):  # בודקים שמספר הרשומה שביקש הלומד קיים בתוצר
        parser.error(f"record must be between 1 and {len(records)}")  # מסבירים איזה ערך חסר או אינו חוקי באפשרויות ההרצה
    record = records[args.record - 1]  # ניגשים לרשומת הקטע שנבדקת או מוצגת כעת
    # הפלט באנגלית כדי למנוע היפוך תוויות בטרמינל. הטקסט המקורי נשמר בעברית.
    print(f"STAGE: {data['stage']}; RECORDS: {len(records)}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    if "manifest" in data:  # בודקים את תנאי הקלט שעליו נשען הענף הבא
        print("MANIFEST:", json.dumps(data["manifest"], ensure_ascii=False))  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print(f"RECORD: {args.record}; CHUNK_ID: {record['chunk_id']}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print("METADATA:", json.dumps(record["metadata"], ensure_ascii=False, indent=2))  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print(f"SOURCE_SPAN: [{record['start']}:{record['end']}]")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print("FULL CHUNK TEXT:\n" + record["text"])  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    vector = record.get("embedding")  # שומרים את vector עבור המשך הפעולה
    if vector is None:  # מבחינים בין קטע מוכן לבין קטע שכבר חושב עבורו embedding
        print("EMBEDDING: not created yet; prepare stage contains text and metadata")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    else:  # מפעילים את המסלול החלופי עבור מצב הקלט הזה
        print("VECTOR_DIMENSIONS:", len(vector))  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        print("VECTOR:" if args.full_vector else "VECTOR_PREVIEW_FIRST_8:", vector if args.full_vector else vector[:8])  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    if args.html:  # יוצרים גם תצוגת דפדפן רק אם נבחר קובץ HTML לפלט
        # כל התוכן עובר escaping לפני הצגה, כולל מסמכים שהלומדים עשויים לערוך.
        cards = []  # אוספים תיבות HTML להצגת כל הקטעים
        for number, item in enumerate(records, 1):  # עוברים לפי הסדר על number, item ומטפלים בכל פריט בנפרד
            metadata = html.escape(json.dumps(item["metadata"], ensure_ascii=False, indent=2))  # שומרים שדות המאפשרים לזהות מקור וגרסה
            values = item.get("embedding", [])  # שומרים את values עבור המשך הפעולה
            vector_json = html.escape(json.dumps(values))  # שומרים את vector_json עבור המשך הפעולה
            source = html.escape(item["metadata"]["source"])  # שומרים את source עבור המשך הפעולה
            cards.append(f'<section id="r{number}"><h2>Chunk {number}</h2><p dir="ltr">{source}</p>'  # מוסיפים את התוצאה לרשימה תוך שמירה על סדר העיבוד
                         f'<details><summary>Metadata</summary><pre>{metadata}</pre></details>'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                         f'<h3>טקסט הקטע</h3><pre class="text">{html.escape(item["text"])}</pre>'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                         f'<p>מספר ממדים: {len(values)}</p><pre>{html.escape(json.dumps(values[:8]))}</pre>'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                         f'<details><summary>הווקטור המלא</summary><pre>{vector_json}</pre></details></section>')  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
        mode = data.get("manifest", {}).get("embedding_mode", "chunks_only")  # קוראים אם מדובר בספק אמיתי או ב־fixture לבדיקה
        page = ('<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">'  # בונים עמוד HTML מקומי לעיון בתוצר
                '<meta name="viewport" content="width=device-width, initial-scale=1">'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                '<title>Index viewer</title><style>body{font:17px Arial;line-height:1.7;background:#eef2f5;'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                'color:#18313d;margin:auto;max-width:1000px;padding:24px}section{background:white;'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                'padding:22px;margin:20px 0;border-radius:12px}pre{direction:ltr;text-align:left;'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                'white-space:pre-wrap;overflow-wrap:anywhere}.text{direction:rtl;text-align:right}'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                'summary{cursor:pointer}</style><h1>הקטעים וה־embeddings שנשמרו</h1>'  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
                f'<p dir="ltr">Mode: {html.escape(mode)} | Records: {len(records)}</p>' + ''.join(cards))  # משלימים את ביטוי הנתונים של main תוך שמירה על המבנה המוצג
        output = Path(args.html)  # מכינים את תוצר השלב לפני שמירתו
        output.parent.mkdir(parents=True, exist_ok=True)  # מכינים תיקיית תוצרים אם היא עדיין אינה קיימת
        output.write_text(page, encoding="utf-8")  # שומרים את התוצר בקובץ כדי שאפשר יהיה לפתוח ולהעביר אותו לשלב הבא
        print("HTML_VIEWER:", output.resolve())  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה


if __name__ == "__main__":  # מפעילים את התוכנית כאשר מריצים את הקובץ עצמו
    main()  # משלימים את ביטוי הנתונים של inspect_index תוך שמירה על המבנה המוצג
