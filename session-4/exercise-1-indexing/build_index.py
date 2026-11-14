"""מריצים את הקובץ בשני שלבים: prepare ואז embed.

prepare מפעיל את הפונקציות שבונים ב־chunking.py ושומר טקסט ומקורות.
embed שולח כל chunk למודל embeddings ושומר לידו את הווקטור שהתקבל.
"""
import argparse  # טוענים כלי עבור קריאת אפשרויות משורת הפקודה
import importlib.util  # טוענים כלי עבור טעינת מימוש הלומדים מקובץ
import json  # טוענים כלי עבור קריאה וכתיבה של קבצי התוצרים
import sys  # טוענים כלי עבור דיווח קוד סיום והודעות שגיאה
import time  # טוענים כלי עבור מדידת משך ההכנה
from pathlib import Path  # משתמשים בנתיבים שמאפשרים לפתוח ולשמור את קבצי התרגיל
from index_support import (BASE, REQUIRED, digest, load_documents, validate_pieces,  # טוענים את פונקציות העזר הדרושות לשלב הנוכחי
                           resolve_model, embed_texts, fixture_vectors, validate_vectors)  # משלימים את ביטוי הנתונים של build_index תוך שמירה על המבנה המוצג


def prepare(args):  # מכינים קטעי מקור עם metadata לפני יצירת embeddings
    # טוענים את המימוש שבחרנו. ברירת המחדל היא הקובץ שהסטודנטים משלימים.
    path = Path(args.implementation).resolve()  # שומרים את הנתיב לקובץ שהפעולה הנוכחית קוראת או כותבת
    spec = importlib.util.spec_from_file_location("student_chunking", path)  # שומרים את spec עבור המשך הפעולה
    module = importlib.util.module_from_spec(spec)  # שומרים את module עבור המשך הפעולה
    spec.loader.exec_module(module)  # משלימים את ביטוי הנתונים של prepare תוך שמירה על המבנה המוצג
    documents = load_documents(args.documents)  # טוענים את מסמכי המקור לתרגול
    records = []  # שומרים את רשומות הקטעים לשלב הבא
    for document in documents:  # עוברים לפי הסדר על document ומטפלים בכל פריט בנפרד
        splitter = module.split_document_structured if args.structured else module.split_document  # בונוס 1: חלוקה לפי מבנה
        pieces = splitter(document, args.max_chars)  # מכינים את הקטעים שהחלוקה החזירה עבור מסמך אחד
        validate_pieces(document, pieces, args.max_chars)  # משלימים את ביטוי הנתונים של prepare תוך שמירה על המבנה המוצג
        for number, piece in enumerate(pieces, 1):  # עוברים לפי הסדר על number, piece ומטפלים בכל פריט בנפרד
            metadata = module.make_metadata(document, piece)  # שומרים שדות המאפשרים לזהות מקור וגרסה
            for field in REQUIRED:  # עוברים לפי הסדר על field ומטפלים בכל פריט בנפרד
                if metadata.get(field) != document[field]:  # בודקים שה־metadata של הקטע תואם לזהות המסמך המקורי
                    raise ValueError(f"Metadata mismatch: {field} in {document['source']}")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
            # ה־ID מחבר מקור, גרסה, מיקום ותוכן. metadata נשמר ליד הטקסט.
            identity = f"{document['source']}|{document['version']}|{piece['start']}|{piece['text']}"  # מחברים מקור, גרסה ומיקום למזהה הקטע
            records.append({"chunk_id": digest(identity)[:20], "text": piece["text"],  # מוסיפים את התוצאה לרשימה תוך שמירה על סדר העיבוד
                            "start": piece["start"], "end": piece["end"],  # משלימים את ביטוי הנתונים של prepare תוך שמירה על המבנה המוצג
                            "metadata": metadata, "document_sha256": document["document_sha256"]})
            if "embedding_text" in piece:  # בונוס 1: טקסט נפרד למודל עם כותרות סעיף/טבלה; text נשאר span מהמקור
                records[-1]["embedding_text"] = piece["embedding_text"]  # נשמר ליד הרשומה ונשלח למודל בשלב embed  # משלימים את ביטוי הנתונים של prepare תוך שמירה על המבנה המוצג
        print(f"DOCUMENT {document['document_id']} v{document['version']}: {len(pieces)} chunks")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    # כתיבה לקובץ UTF-8 משמרת עברית קריאה גם מחוץ ל־Python.
    output = Path(args.output)  # מכינים את תוצר השלב לפני שמירתו
    output.parent.mkdir(parents=True, exist_ok=True)  # מכינים תיקיית תוצרים אם היא עדיין אינה קיימת
    output.write_text(json.dumps({"schema_version": 1, "stage": "chunks", "document_count": len(documents),  # שומרים את התוצר בקובץ כדי שאפשר יהיה לפתוח ולהעביר אותו לשלב הבא
                                  "chunking": {"max_chars": args.max_chars, "structured": bool(args.structured)}, "records": records},  # משלימים את ביטוי הנתונים של prepare תוך שמירה על המבנה המוצג
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")  # שומרים את ensure_ascii עבור המשך הפעולה
    print(f"PREPARED: documents={len(documents)}, chunks={len(records)}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print(f"SAVED: {output.resolve()}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print("NEXT: inspect chunks, then run the embed command from README-HE.html")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה


def embed(args):  # מוסיפים embedding לכל קטע ושומרים את מאפייני המודל
    started = time.perf_counter()  # שומרים את זמן תחילת ההרצה
    prepared = json.loads(Path(args.input).read_text(encoding="utf-8"))  # טוענים את הקטעים שהוכנו לפני חישוב embeddings
    if prepared.get("stage") != "chunks" or not prepared.get("records"):  # דורשים קובץ קטעים שהכנתו הסתיימה לפני יצירת embeddings
        raise ValueError("Input must be a non-empty prepared chunks file")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    records = prepared["records"]  # שומרים את רשומות הקטעים לשלב הבא
    document_format = args.document_format or ("embeddinggemma" if args.provider == "ollama" else "none")  # קובעים איזה prefix יתווסף לטקסט לפני יצירת embedding
    requested_model = args.model or "embeddinggemma"  # בוחרים את שם המודל מהפקודה או מברירת המחדל של הספק
    # Prefix המותאם ל־EmbeddingGemma מציין שהקלט הוא מסמך לאינדוקס.
    # במודל אחר בוחרים none לאחר בדיקת הוראות המודל שלו.
    texts = []  # אוספים את טקסטי הקלט למודל לפי סדר הרשומות
    for record in records:  # עוברים לפי הסדר על record ומטפלים בכל פריט בנפרד
        text = record.get("embedding_text", record["text"])  # בונוס 1: אם יש הקשר נוסף, הוא חלק מהקלט למודל
        if document_format == "embeddinggemma":  # בוחרים את עיצוב הקלט שהמודל דורש לפי ה־index שנבנה קודם
            text = f"title: {record['metadata']['title']} | text: {text}"  # שומרים את הטקסט המדויק שמעובד בשלב הזה
        texts.append(text)  # מוסיפים את התוצאה לרשימה תוך שמירה על סדר העיבוד
    if args.fixture:  # מפרידים בין מספרי בדיקה מקומיים לבין פלט מודל אמיתי
        model, model_digest = "OFFLINE-FIXTURE-NOT-SEMANTIC", "not-a-model"  # משלימים את ביטוי הנתונים של embed תוך שמירה על המבנה המוצג
        vectors = fixture_vectors(texts)  # אוספים את הווקטורים שהתקבלו עבור הקלטים
    else:  # מפעילים את המסלול החלופי עבור מצב הקלט הזה
        model, model_digest = resolve_model(args.base_url, requested_model)  # בודקים שהמודל מותקן ב־Ollama ושומרים את ה־digest שלו
        signature = {"embedding_mode": args.provider, "model": model, "model_digest": model_digest,  # בונוס 2: מה חייב להיות זהה כדי לשמור וקטור ישן
                     "document_format": document_format}  # שינוי בכל אחד מהם מבטל שימוש חוזר
        cache, reason = load_reuse_cache(args.reuse, signature)  # מפת hash של קלט ← וקטור, מתוך index קודם תואם
        if args.reuse:  # מדווחים אם ה־index הקודם שמיש
            print(f"REUSE: {reason}")  # למשל model changed → מחשבים הכול מחדש
        vectors = [cache.get(digest(text)) for text in texts]  # וקטור ישן רק אם הקלט המדויק למודל לא השתנה
        missing = [i for i, vector in enumerate(vectors) if vector is None]  # רק אלה נשלחים למודל
        # batches קטנים מקלים על זיהוי שגיאות ומציגים התקדמות בהרצה מקומית.
        for start in range(0, len(missing), 8):  # עוברים לפי הסדר על start ומטפלים בכל פריט בנפרד
            positions = missing[start:start + 8]  # מיקומי הטקסטים בקבוצה הנוכחית
            batch = [texts[i] for i in positions]  # בוחרים קבוצת טקסטים קטנה לבקשה אחת
            fresh = embed_texts(args.base_url, model, batch)  # וקטורים חדשים מ־Ollama
            for i, vector in zip(positions, fresh):  # מציבים כל וקטור במקום של הרשומה שלו
                vectors[i] = vector  # שומרים על סדר הרשומות
            print(f"EMBEDDED: {start + len(batch)}/{len(missing)}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        if args.reuse:  # counters לבדיקת העדכון החלקי
            report_reuse(args.reuse, records, texts, cache, len(missing))  # שמורים / חושבו / הוזזו / הוסרו
    dimension = validate_vectors(vectors)  # קובעים את מספר הממדים מתוך הווקטורים שהוחזרו
    for record, text, vector in zip(records, texts, vectors):  # עוברים לפי הסדר על record, text, vector ומטפלים בכל פריט בנפרד
        record["embedding"] = vector  # משלימים את ביטוי הנתונים של embed תוך שמירה על המבנה המוצג
        record["embedding_input_sha256"] = digest(text)  # משלימים את ביטוי הנתונים של embed תוך שמירה על המבנה המוצג
    # ה־manifest מתעד את המודל ואת הממדים לכל ה־index. אין כאן Database.
    result = {"schema_version": 1, "stage": "index", "manifest": {  # שומרים את תוצאת הפעולה לצורך אימות או המשך עיבוד
        "embedding_mode": "offline_fixture" if args.fixture else args.provider,  # משלימים את ביטוי הנתונים של embed תוך שמירה על המבנה המוצג
        "model": model, "model_digest": model_digest, "dimensions": dimension,  # משלימים את ביטוי הנתונים של embed תוך שמירה על המבנה המוצג
        "document_format": document_format, "document_count": prepared["document_count"],  # משלימים את ביטוי הנתונים של embed תוך שמירה על המבנה המוצג
        "chunk_count": len(records), "chunking": prepared["chunking"]}, "records": records}  # משלימים את ביטוי הנתונים של embed תוך שמירה על המבנה המוצג
    output = Path(args.output or ("build/offline-index.json" if args.fixture else "build/index.json"))  # מכינים את תוצר השלב לפני שמירתו
    if args.fixture and output.name == "index.json":  # מפרידים בין מספרי בדיקה מקומיים לבין פלט מודל אמיתי
        raise ValueError("Use a distinct offline filename, such as build/offline-index.json")  # עוצרים עם הסבר ברור כאשר החוזה או הקלט אינם תקינים
    output.parent.mkdir(parents=True, exist_ok=True)  # מכינים תיקיית תוצרים אם היא עדיין אינה קיימת
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")  # שומרים את התוצר בקובץ כדי שאפשר יהיה לפתוח ולהעביר אותו לשלב הבא
    print(f"MODE: {result['manifest']['embedding_mode']}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print(f"INDEX: documents={prepared['document_count']}, chunks={len(records)}, dimensions={dimension}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print(f"MODEL: {model}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print(f"SAVED: {output.resolve()}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    print(f"ELAPSED_SECONDS: {time.perf_counter()-started:.2f}")  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
    if args.fixture:  # מפרידים בין מספרי בדיקה מקומיים לבין פלט מודל אמיתי
        print("FIXTURE ONLY: these vectors do not demonstrate Semantic Search or real embeddings")
    elif not args.no_atlas:  # שומרים את ה־index גם ב־MongoDB Atlas, אם יש קובץ הגדרות
        save_to_atlas(result, args.atlas_env)  # העלאה + Vector Search Index


def save_to_atlas(result, env_path):  # מעלים את ה־index ל־Atlas לפי atlas-credentials.env / .env
    import atlas_upload  # הקוד של Atlas נטען רק כאן; pymongo נדרש רק למסלול הזה
    settings, problem = atlas_upload.atlas_settings(BASE, env_path)  # פרטי החיבור מהקובץ או מהסביבה
    if problem:  # אין פרטי חיבור: ה־index נשמר מקומית בלבד
        print(f"ATLAS: skipped — {problem}")  # רק שמות מפתחות, לא ערכים
        return  # לא נכשלים; הקובץ המקומי כבר נשמר
    print(f"ATLAS: saving to MongoDB using credentials from {settings['source']}")  # בלי להדפיס את ה־URI
    try:  # שגיאת רשת/הרשאה לא מבטלת את ה־index המקומי
        atlas_upload.sync_index(result, settings)  # upsert + מחיקת קטעים ישנים + Vector Index
    except Exception as error:  # כולל שגיאות pymongo
        raise ValueError("Atlas upload failed (local index was saved): " + atlas_upload.safe_error(error, settings)) from None  # הודעה בלי סיסמה  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה


def load_reuse_cache(path, signature):  # בונוס 2: טוענים וקטורים מ־index קודם רק אם הוא תואם
    if not path:  # לא התבקש עדכון חלקי
        return {}, "disabled"  # מחשבים הכול
    old_path = Path(path)  # ה־index הקודם
    if not old_path.exists():  # הרצה ראשונה
        return {}, f"no previous index at {old_path}; embedding everything"  # אין ממה לשמור
    old = json.loads(old_path.read_text(encoding="utf-8"))  # קוראים את ה־index הקודם
    manifest = old.get("manifest", {})  # מאפייני המודל שבו חושבו הווקטורים הישנים
    for field, value in signature.items():  # ספק, מודל, digest ופורמט קלט חייבים להיות זהים
        if manifest.get(field) != value:  # וקטור ממודל אחר אינו באותו מרחב
            return {}, f"{field} changed ({manifest.get(field)!r} -> {value!r}); embedding everything"  # מבטלים שימוש חוזר
    cache = {}  # hash של הקלט המדויק למודל ← וקטור
    for record in old.get("records", []):  # כל רשומה ישנה
        if record.get("embedding_input_sha256") and record.get("embedding"):  # רק רשומות שלמות
            cache[record["embedding_input_sha256"]] = record["embedding"]  # אותו קלט ← אותו וקטור
    dimensions = {len(vector) for vector in cache.values()}  # כל הווקטורים הישנים חייבים להיות באותו ממד
    if len(dimensions) > 1 or (cache and manifest.get("dimensions") not in dimensions):  # index פגום
        return {}, "previous index has inconsistent dimensions; embedding everything"  # לא מסתכנים בערבוב ממדים
    return cache, f"compatible previous index with {len(cache)} vectors"  # מותר להשתמש בווקטורים


def report_reuse(path, records, texts, cache, embedded):  # בונוס 2: counters של העדכון החלקי
    old_path = Path(path)  # ה־index הקודם
    old_records = json.loads(old_path.read_text(encoding="utf-8")).get("records", []) if old_path.exists() else []  # רשומות ישנות
    old_by_hash = {record.get("embedding_input_sha256"): record for record in old_records}  # מאתרים רשומה ישנה לפי הקלט
    new_hashes = {digest(text) for text in texts}  # הקלטים בהרצה הנוכחית
    reused = moved = 0  # נשמרו / נשמרו אך המיקום או ה־metadata השתנו
    for record, text in zip(records, texts):  # כל רשומה חדשה
        key = digest(text)  # hash של הקלט למודל
        if key in cache:  # הווקטור נלקח מה־index הקודם
            reused += 1  # נספר כשמור
            old = old_by_hash[key]  # הרשומה הישנה עם אותו קלט
            if (old["start"], old["end"], old["metadata"]) != (record["start"], record["end"], record["metadata"]):  # למשל פסקה שנוספה לפניה
                moved += 1  # עודכנו מיקום/metadata בלי חישוב חדש
    removed = sum(1 for record in old_records if record.get("embedding_input_sha256") not in new_hashes)  # קטעים שנמחקו או השתנו
    print(f"REUSE_COUNTERS: reused={reused} (moved_or_metadata_updated={moved}), embedded={embedded}, removed={removed}")  # תוצאה לבדיקה


def main():  # מגדירים אפשרויות הרצה ומפעילים את המסלול שהלומד בחר
    # שתי פקודות מופרדות מאפשרות לבדוק את הטקסט לפני שמפעילים מודל.
    parser = argparse.ArgumentParser(description="Exercise 1: document indexing, no retrieval")  # מכינים את קורא אפשרויות ההרצה
    sub = parser.add_subparsers(dest="command", required=True)  # מפרידים בין הכנת הקטעים לבין יצירת embeddings
    p = sub.add_parser("prepare")  # מגדירים פקודה נפרדת לשלב בתהליך האינדוקס
    p.add_argument("--documents", default=str(BASE / "documents"))  # מגדירים אפשרות הרצה: --documents
    p.add_argument("--implementation", default=str(BASE / "chunking.py"))  # מגדירים אפשרות הרצה: --implementation
    p.add_argument("--max-chars", type=int, default=900)  # מגדירים אפשרות הרצה: --max-chars
    p.add_argument("--output", default="build/chunks.json")
    p.add_argument("--structured", action="store_true", help="Bonus 1: keep headings with content and split tables between rows")  # בונוס 1  # מגדירים אפשרות הרצה: --output
    p.set_defaults(function=prepare)  # מחברים את הפקודה לפונקציה שמבצעת את השלב
    e = sub.add_parser("embed")  # מגדירים פקודה נפרדת לשלב בתהליך האינדוקס
    e.add_argument("--input", default="build/chunks.json")  # מגדירים אפשרות הרצה: --input
    e.add_argument("--output")  # מגדירים אפשרות הרצה: --output
    e.add_argument("--provider", choices=("ollama",), default="ollama")  # מגדירים אפשרות הרצה: --provider
    e.add_argument("--model")  # מגדירים אפשרות הרצה: --model
    e.add_argument("--base-url", default="http://127.0.0.1:11434")  # מגדירים אפשרות הרצה: --base-url
    e.add_argument("--document-format", choices=("embeddinggemma", "none"))  # מגדירים אפשרות הרצה: --document-format
    e.add_argument("--fixture", action="store_true")
    e.add_argument("--no-atlas", action="store_true", help="Do not upload to MongoDB Atlas even if credentials exist")  # רק קובץ מקומי
    e.add_argument("--atlas-env", help="Atlas credentials file (default: atlas-credentials.env or .env in this folder)")  # קובץ הגדרות
    e.add_argument("--reuse", metavar="OLD_INDEX", help="Bonus 2: reuse vectors from a previous compatible index")  # בונוס 2  # מגדירים אפשרות הרצה: --fixture
    e.set_defaults(function=embed)  # מחברים את הפקודה לפונקציה שמבצעת את השלב
    args = parser.parse_args()  # קוראים את אפשרויות ההרצה שנבחרו
    if hasattr(args, "max_chars") and args.max_chars < 100:  # בודקים שגודל הקטע או מגבלת החלוקה עומדים בחוזה התרגיל
        parser.error("max-chars must be at least 100")  # מסבירים איזה ערך חסר או אינו חוקי באפשרויות ההרצה
    try:  # מפעילים את השלב עם טיפול מרוכז בשגיאות צפויות
        args.function(args)  # מפעילים את שלב prepare או embed שנבחר בפקודה
    except (ValueError, KeyError, OSError, NotImplementedError, TypeError) as error:  # מתרגמים שגיאה צפויה להודעה שמסבירה מה צריך לתקן
        print(f"STOP: {error}", file=sys.stderr)  # מציגים ללומד את תוצאת השלב ואת המידע הדרוש לבדיקה
        return 2  # מחזירים קוד כשל כדי שהטרמינל יציג שהשלב נעצר
    return 0  # מסמנים שהפעולה הסתיימה בהצלחה


if __name__ == "__main__":  # מפעילים את התוכנית כאשר מריצים את הקובץ עצמו
    raise SystemExit(main())  # מסיימים את התוכנית עם קוד הסיום שהחזירה פונקציית ההפעלה
