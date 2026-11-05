"""חלקים מסופקים: קריאת מסמכים, אימות נתונים וקריאה ל־Ollama.

אין כאן חיפוש או יצירת תשובה. הפונקציות תומכות בהכנת index מקומי בקובץ JSON.
"""
import hashlib  # טוענים כלי עבור זיהוי תוכן באמצעות hash
import json  # טוענים כלי עבור קריאה וכתיבה של קבצי התוצרים
import math  # טוענים כלי עבור חישוב אורכי וקטורים ובדיקת מספרים סופיים
import os  # טוענים כלי עבור קריאת משתני הסביבה בלי לשמור מפתח בקובץ
import re  # טוענים כלי עבור פיצול טקסט וקריאת שדות המקור
import urllib.error  # טוענים כלי עבור זיהוי שגיאת חיבור או API
import urllib.request  # טוענים כלי עבור שליחת בקשות HTTP לספק embeddings
from pathlib import Path  # משתמשים בנתיבים שמאפשרים לפתוח ולשמור את קבצי התרגיל

BASE = Path(__file__).resolve().parent  # שומרים את BASE עבור המשך הפעולה
REQUIRED = ("document_id", "version", "status", "updated", "source", "title")  # שומרים את REQUIRED עבור המשך הפעולה


def load_documents(directory):  # קוראים את קבצי המקור ואת שדות הזהות והגרסה שלהם
    # קוראים רק מסמכי Markdown מתוך תיקיית המסמכים שנבחרה.
    # README וקובצי תוצר אינם מקורות לאינדוקס.
    documents = []  # טוענים את מסמכי המקור לתרגול
    for path in sorted(Path(directory).glob("*.md")):  # עוברים לפי הסדר על path ומטפלים בכל פריט בנפרד
        text = path.read_text(encoding="utf-8")  # שומרים את הטקסט המדויק שמעובד בשלב הזה
        paragraphs = text.split("\n\n", 2)  # שומרים את paragraphs עבור המשך הפעולה
        if len(paragraphs) < 3 or not text.startswith("# "):  # מאמתים שהטקסט ומיקומו מתאימים בדיוק למסמך המקורי
            raise ValueError(f"Missing title/metadata/body: {path.name}")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
        raw = dict(re.findall(r"^([A-Za-z ]+): (.+)$", paragraphs[1], re.M))  # קוראים נתוני מקור לפני ההמרה למבנה התרגיל
        documents.append({  # מוסיפים את התוצאה לרשימה תוך שמירה על סדר העיבוד
            "document_id": raw["Document ID"], "version": raw["Version"],  # משלימים את ביטוי הנתונים של load_documents תוך שמירה על המבנה המוצג
            "status": raw["Status"], "updated": raw["Updated"],  # משלימים את ביטוי הנתונים של load_documents תוך שמירה על המבנה המוצג
            "source": "documents/" + path.name, "title": paragraphs[0][2:].strip(),  # משלימים את ביטוי הנתונים של load_documents תוך שמירה על המבנה המוצג
            "body": paragraphs[2], "document_sha256": digest(text),  # משלימים את ביטוי הנתונים של load_documents תוך שמירה על המבנה המוצג
        })  # משלימים את ביטוי הנתונים של load_documents תוך שמירה על המבנה המוצג
    if not documents:  # בודקים את תנאי הקלט שעליו נשען הענף הבא
        raise ValueError("No .md source documents found")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    return documents  # מחזירים את תוצאת load_documents לקוד שקרא לפונקציה


def digest(text):  # מחשבים מזהה תוכן לצורך השוואת גרסאות וקטעים
    # מזהה תוכן מאפשר להבחין בין שני טקסטים גם אם שם הקובץ נשאר זהה.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()  # מחזירים את תוצאת digest לקוד שקרא לפונקציה


def validate_pieces(document, pieces, max_chars):  # בודקים שהחלוקה שומרת על הטקסט ועל המיקומים המקוריים
    # בודקים את תוצאת הבנייה, כדי שבאג בחלוקה לא יישמר בשקט בתוך index.
    if not pieces:  # דורשים שלכל מסמך יוחזר לפחות קטע אחד
        raise ValueError(f"No chunks: {document['source']}")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    covered = set()  # עוקבים אחרי התווים שכוסו בחלוקה
    previous_start = -1  # זוכרים את מיקום הקטע הקודם כדי לבדוק סדר
    for piece in pieces:  # עוברים לפי הסדר על piece ומטפלים בכל פריט בנפרד
        start, end, text = piece["start"], piece["end"], piece["text"]  # משלימים את ביטוי הנתונים של validate_pieces תוך שמירה על המבנה המוצג
        if not (isinstance(start, int) and isinstance(end, int)):  # מאמתים שהטקסט ומיקומו מתאימים בדיוק למסמך המקורי
            raise ValueError("Chunk offsets must be integers")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
        if not (0 <= start < end <= len(document["body"])) or start < previous_start:  # מאמתים שהטקסט ומיקומו מתאימים בדיוק למסמך המקורי
            raise ValueError("Chunk offsets are invalid or out of order")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
        if document["body"][start:end] != text or not text.strip():  # מאמתים שהטקסט ומיקומו מתאימים בדיוק למסמך המקורי
            raise ValueError("Chunk text must match the original source span")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
        if len(text) > max_chars:  # בודקים שגודל הקטע או מגבלת החלוקה עומדים בחוזה התרגיל
            raise ValueError(f"Chunk exceeds max_chars={max_chars}")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
        previous_start = start  # זוכרים את מיקום הקטע הקודם כדי לבדוק סדר
        covered.update(range(start, end))  # מעדכנים את קבוצת הערכים שנאספו לצורך בדיקה
    missing = [i for i, char in enumerate(document["body"]) if not char.isspace() and i not in covered]  # מאתרים תוכן מהמקור שלא נשמר בקטעים
    if missing:  # מזהים טקסט בעל תוכן שלא נשמר באף קטע
        raise ValueError(f"Chunking omitted source text near character {missing[0]}")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים


def http_json(base_url, path, payload=None, timeout=180):  # מרכזים קריאת JSON משרת Ollama וטיפול בשגיאות חיבור
    # urllib הוא חלק מ־Python: אין התקנת SDK או חבילה חיצונית בתרגיל.
    data = None if payload is None else json.dumps(payload).encode("utf-8")  # קוראים את נתוני התוצר או תשובת ה־API
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data,  # מכינים בקשת HTTP עם גוף JSON וכותרות נדרשות
                                     headers={"Content-Type": "application/json"})  # שומרים את headers עבור המשך הפעולה
    try:  # מפעילים את השלב עם טיפול מרוכז בשגיאות צפויות
        with urllib.request.urlopen(request, timeout=timeout) as response:  # פותחים משאב לזמן הפעולה וסוגרים אותו בסיומה
            return json.load(response)  # מחזירים את תוצאת http_json לקוד שקרא לפונקציה
    except urllib.error.HTTPError as error:  # מתרגמים שגיאה צפויה להודעה שמסבירה מה צריך לתקן
        detail = error.read().decode("utf-8", errors="replace")[:500]  # שומרים את detail עבור המשך הפעולה
        raise ValueError(f"Ollama HTTP {error.code}: {detail}") from None  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    except urllib.error.URLError:  # מתרגמים שגיאה צפויה להודעה שמסבירה מה צריך לתקן
        raise ValueError("Cannot reach Ollama. Start Ollama, then run: ollama list") from None  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים


def resolve_model(base_url, model):  # מאתרים מודל מותקן ואת מזהה המשקולות שלו
    # מזהים את המודל המותקן ואת digest המשקולות, בלי להוריד מודל חדש.
    for item in http_json(base_url, "/api/tags")["models"]:  # עוברים לפי הסדר על item ומטפלים בכל פריט בנפרד
        if item["name"] == model or item["name"] == model + ":latest":  # מאתרים ברשימת Ollama את שם המודל המבוקש ואת תגיתו
            return item["name"], item["digest"]  # מחזירים את תוצאת resolve_model לקוד שקרא לפונקציה
    raise ValueError(f"Model not installed: {model}. Preparation command: ollama pull {model}")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים


def embed_texts(base_url, model, texts):  # שולחים טקסטים למודל Ollama ומקבלים וקטור לכל טקסט
    # שליחת כמה טקסטים באותה קריאה מחזירה וקטור נפרד לכל טקסט.
    # truncate=False גורם לשגיאה כשהקלט ארוך מדי, במקום לחתוך אותו בשקט.
    result = http_json(base_url, "/api/embed", {"model": model, "input": texts, "truncate": False})  # שומרים את תוצאת הפעולה לצורך אימות או המשך עיבוד
    vectors = result.get("embeddings", [])  # אוספים את הווקטורים שהתקבלו עבור הקלטים
    if len(vectors) != len(texts):  # דורשים וקטור אחד לכל טקסט שנשלח למודל
        raise ValueError("Ollama returned a different number of vectors than inputs")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    return vectors  # מחזירים את תוצאת embed_texts לקוד שקרא לפונקציה


def embed_voyage(model, texts, input_type="document"):  # שולחים בקשת embeddings לענן (Voyage AI) עם מפתח שנקרא מהסביבה
    # ל־Anthropic אין endpoint של embeddings; Anthropic ממליצה על Voyage AI למסלול הענן.
    # המסלול בענן משתמש במפתח מהסביבה. לא כותבים או מדפיסים אותו.
    # הפעלת הפונקציה מבצעת בקשת API שעשויה להיות בתשלום.
    # input_type מבדיל בין מסמך לאינדוקס ("document") לבין שאלת חיפוש ("query").
    key = os.environ.get("VOYAGE_API_KEY", "").strip()  # קוראים מפתח API מהסביבה בלי להדפיס אותו
    if not key:  # עוצרים לפני בקשת ענן כאשר חסר מפתח API
        raise ValueError("VOYAGE_API_KEY is missing. See the cloud setup section in README-HE.html")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    request = urllib.request.Request(  # מכינים בקשת HTTP עם גוף JSON וכותרות נדרשות
        "https://api.voyageai.com/v1/embeddings",  # משלימים את ביטוי הנתונים של embed_voyage תוך שמירה על המבנה המוצג
        data=json.dumps({"model": model, "input": texts, "input_type": input_type, "truncation": False}).encode("utf-8"),  # truncation=False: קלט ארוך מדי מחזיר שגיאה במקום להיחתך בשקט
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})  # שומרים את headers עבור המשך הפעולה
    try:  # מפעילים את השלב עם טיפול מרוכז בשגיאות צפויות
        with urllib.request.urlopen(request, timeout=180) as response:  # פותחים משאב לזמן הפעולה וסוגרים אותו בסיומה
            result = json.load(response)  # שומרים את תוצאת הפעולה לצורך אימות או המשך עיבוד
    except urllib.error.HTTPError as error:  # מתרגמים שגיאה צפויה להודעה שמסבירה מה צריך לתקן
        # הודעת שגיאה גולמית עלולה להכיל חלק ממפתח. מציגים רק קוד והנחיה.
        raise ValueError(f"Voyage AI HTTP {error.code}. Check API key, model name, billing and network settings.") from None  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    except urllib.error.URLError:  # מתרגמים שגיאה צפויה להודעה שמסבירה מה צריך לתקן
        raise ValueError("Cannot reach Voyage AI. Check network/proxy settings.") from None  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    data = sorted(result.get("data", []), key=lambda item: item["index"])  # קוראים את נתוני התוצר או תשובת ה־API
    if [item["index"] for item in data] != list(range(len(texts))):  # בודקים שכל קלט קיבל וקטור ושסדר הרשומות בתשובה שלם
        raise ValueError("Voyage AI returned missing or duplicate embedding indexes")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    return [item["embedding"] for item in data]  # מחזירים את תוצאת embed_voyage לקוד שקרא לפונקציה


def validate_vectors(vectors):  # בודקים מספר ממדים וערכים תקינים לפני שמירת הווקטורים
    # לכל הרשומות חייב להיות אותו מספר ממדים וערכים מספריים סופיים.
    if not vectors or not vectors[0]:  # דורשים פלט וקטורי שאינו ריק לפני שמירת index
        raise ValueError("Empty embedding output")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    dimension = len(vectors[0])  # קובעים את מספר הממדים מתוך הווקטורים שהוחזרו
    for vector in vectors:  # עוברים לפי הסדר על vector ומטפלים בכל פריט בנפרד
        if len(vector) != dimension or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector):  # בודקים שכל הווקטורים באותו אורך ומכילים מספרים סופיים
            raise ValueError("Invalid or inconsistent embedding vectors")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    return dimension  # מחזירים את תוצאת validate_vectors לקוד שקרא לפונקציה


def fixture_vectors(texts):  # מכינים מספרי בדיקה דטרמיניסטיים ללא משמעות סמנטית
    # מצב בדיקה בלבד: מספרים דטרמיניסטיים מתוך hash, ללא משמעות סמנטית.
    # משתמשים בו לאימות קבצים וחוזי נתונים כאשר Ollama אינו זמין.
    vectors = []  # אוספים את הווקטורים שהתקבלו עבור הקלטים
    for text in texts:  # עוברים לפי הסדר על text ומטפלים בכל פריט בנפרד
        raw = bytes.fromhex(digest(text))  # קוראים נתוני מקור לפני ההמרה למבנה התרגיל
        vectors.append([(byte - 127.5) / 127.5 for byte in raw[:8]])  # מוסיפים את התוצאה לרשימה תוך שמירה על סדר העיבוד
    return vectors  # מחזירים את תוצאת fixture_vectors לקוד שקרא לפונקציה
