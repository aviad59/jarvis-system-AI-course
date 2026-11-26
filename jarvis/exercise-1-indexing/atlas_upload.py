"""מעלה את build/index.json ל־MongoDB Atlas ויוצר Vector Search Index.

בדרך כלל אין צורך להריץ אותו ישירות: build_index.py embed מעלה אוטומטית כשיש קובץ הגדרות.
הרצה ידנית: python atlas_upload.py
דרוש: pip install pymongo, ופרטי Atlas בקובץ atlas-credentials.env (או .env) בתיקייה זו.

הסנכרון מלא: קטעים מה־index נכתבים (upsert לפי chunk_id) וקטעים שכבר אינם ב־index נמחקים.
ה־manifest נשמר במסמך נפרד, כדי שהחיפוש ידע באיזה מודל ופורמט לחשב את וקטור השאלה.
"""
import argparse  # קוראים אפשרויות משורת הפקודה
import getpass  # מקלידים connection string בהסתרה
import json  # קוראים את ה־index
import os  # קוראים MONGODB_URI מהסביבה
import sys  # קוד יציאה והודעות שגיאה
import time  # ממתינים עד שה־Vector Index מוכן
from pathlib import Path  # נתיב לקובץ ה־index


def connect(uri):  # מתחברים ל־Atlas ובודקים שהחיבור עובד
    try:  # pymongo אינו חלק מהספרייה המובנית
        from pymongo import MongoClient  # הדרייבר הרשמי של MongoDB
    except ImportError:  # הספרייה לא הותקנה
        raise ValueError("pymongo is missing. Run: python -m pip install pymongo") from None  # הנחיה ברורה
    client = MongoClient(uri, serverSelectionTimeoutMS=15000)  # לא ממתינים לנצח אם הכתובת או ה־IP חסומים
    client.admin.command("ping")  # בדיקת חיבור אמיתית
    return client  # חיבור פעיל


def vector_index_definition(dimensions):  # הגדרת אינדקס הווקטורים של Atlas
    return {"fields": [  # שדות האינדקס
        {"type": "vector", "path": "embedding", "numDimensions": dimensions, "similarity": "cosine"},  # אותו מדד כמו בתרגיל 2
        {"type": "filter", "path": "metadata.status"},  # מאפשר לסנן גרסאות superseded בתוך החיפוש
        {"type": "filter", "path": "metadata.document_id"},  # מאפשר לסנן לפי מסמך
        {"type": "filter", "path": "kind"},  # "chat" for chat chunks
        {"type": "filter", "path": "chat_file"},  # chat chunks: which export
        {"type": "filter", "path": "participants"},  # chat chunks: who wrote in it (sender filter)
        {"type": "filter", "path": "start_ts"},  # chat chunks: first message time (time filter)
        {"type": "filter", "path": "end_ts"},  # chat chunks: last message time (time filter)
    ]}  # סוף ההגדרה


def ensure_vector_index(collection, name, dimensions, wait_seconds):  # יוצרים או מעדכנים את האינדקס וממתינים שיהיה מוכן
    from pymongo.operations import SearchIndexModel  # מודל הגדרת אינדקס חיפוש
    definition = vector_index_definition(dimensions)  # ההגדרה הרצויה
    existing = list(collection.list_search_indexes(name))  # האם כבר קיים אינדקס בשם הזה
    if not existing:  # הרצה ראשונה
        collection.create_search_index(SearchIndexModel(definition=definition, name=name, type="vectorSearch"))  # יצירה
        print(f"VECTOR INDEX: creating '{name}' (dimensions={dimensions})")  # דיווח
    elif existing[0].get("latestDefinition") != definition:  # מודל או ממדים השתנו
        collection.update_search_index(name, definition)  # עדכון ההגדרה
        print(f"VECTOR INDEX: updating '{name}' (dimensions={dimensions})")  # דיווח
    else:  # אין שינוי
        print(f"VECTOR INDEX: '{name}' already up to date")  # דיווח
    deadline = time.time() + wait_seconds  # המתנה מוגבלת בזמן
    while time.time() < deadline:  # בודקים מצב עד שהאינדקס זמין לשאילתות
        info = list(collection.list_search_indexes(name))[0]  # מצב נוכחי
        if info.get("queryable") and info.get("status") == "READY":  # מוכן לחיפוש
            print("VECTOR INDEX: READY")  # דיווח
            return  # סיימנו
        time.sleep(5)  # בודקים שוב בעוד כמה שניות
    print(f"VECTOR INDEX: still building after {wait_seconds}s; check Atlas UI (Atlas Search tab) before searching")  # לא נכשלים, רק מתריעים


ENV_FILES = ("atlas-credentials.env", ".env")  # קבצי הגדרות שנבדקים בתיקיית התרגיל, לפי הסדר
URI_KEYS = ("MONGODB_URI", "MONGO_URI", "ATLAS_URI", "MONGODB_CONNECTION_STRING", "ATLAS_CONNECTION_STRING", "MONGODB_URL", "MONGO_URL", "CONNECTION_STRING", "DATABASE_URL")  # שמות מקובלים ל־connection string
DB_KEYS = ("MONGODB_DB", "MONGODB_DATABASE", "MONGO_DB", "MONGO_DATABASE", "ATLAS_DB", "DB_NAME", "DATABASE_NAME", "DATABASE")  # שם מסד הנתונים
COLLECTION_KEYS = ("MONGODB_COLLECTION", "MONGO_COLLECTION", "ATLAS_COLLECTION", "COLLECTION_NAME", "COLLECTION")  # שם האוסף
INDEX_KEYS = ("ATLAS_VECTOR_INDEX", "MONGODB_VECTOR_INDEX", "VECTOR_INDEX_NAME", "VECTOR_INDEX", "INDEX_NAME")  # שם ה־Vector Search Index
USER_KEYS = ("MONGODB_USER", "MONGODB_USERNAME", "MONGO_USER", "MONGO_USERNAME", "ATLAS_USER", "ATLAS_USERNAME", "DB_USER", "DB_USERNAME", "USERNAME", "USER")  # שם משתמש (כשאין URI מלא)
PASSWORD_KEYS = ("MONGODB_PASSWORD", "MONGO_PASSWORD", "ATLAS_PASSWORD", "DB_PASSWORD", "PASSWORD")  # סיסמה (כשאין URI מלא)
HOST_KEYS = ("MONGODB_CLUSTER", "MONGODB_HOST", "MONGO_CLUSTER", "MONGO_HOST", "ATLAS_CLUSTER", "ATLAS_HOST", "CLUSTER", "CLUSTER_URL", "HOST")  # כתובת ה־cluster (כשאין URI מלא)


def read_env_file(path):  # קוראים KEY=VALUE מקובץ .env בלי להדפיס ערכים
    values = {}  # המפתחות והערכים מהקובץ
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():  # כל שורה בקובץ
        line = line.strip()  # מסירים רווחים
        if not line or line.startswith("#") or "=" not in line:  # הערות ושורות ריקות
            continue  # מדלגים
        key, value = line.split("=", 1)  # מפצלים בסימן = הראשון
        key = key.strip().removeprefix("export ").strip()  # תמיכה גם ב־export KEY=...
        value = value.strip()  # מסירים רווחים
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):  # ערך בתוך מרכאות
            value = value[1:-1]  # מסירים את המרכאות
        values[key.upper()] = value  # שמות לא תלויי רישיות
    return values  # מילון הגדרות


def first(values, keys):  # הערך הראשון שקיים מתוך רשימת שמות אפשריים
    return next((values[k] for k in keys if values.get(k)), None)  # None אם אף אחד לא קיים


def atlas_settings(base_dir, env_path=None):  # מאתרים הגדרות Atlas: משתני סביבה גוברים על הקובץ
    files = [Path(env_path)] if env_path else [Path(base_dir) / name for name in ENV_FILES]  # הקובץ שנבחר או ברירות המחדל
    found = next((f for f in files if f.exists()), None)  # הקובץ הראשון שקיים
    values = read_env_file(found) if found else {}  # ערכים מהקובץ
    known = URI_KEYS + DB_KEYS + COLLECTION_KEYS + INDEX_KEYS  # רק משתני סביבה רלוונטיים
    values.update({k.upper(): v for k, v in os.environ.items() if k.upper() in known and v})  # משתני סביבה גוברים
    uri = first(values, URI_KEYS)  # connection string מלא
    if not uri and first(values, PASSWORD_KEYS) and first(values, HOST_KEYS):  # אין URI: מרכיבים מחלקים
        from urllib.parse import quote_plus  # תווים מיוחדים בסיסמה חייבים קידוד
        host = first(values, HOST_KEYS).removeprefix("mongodb+srv://").removeprefix("https://").strip("/")  # רק שם המארח
        user = first(values, USER_KEYS) or ""  # שם משתמש
        uri = f"mongodb+srv://{quote_plus(user)}:{quote_plus(first(values, PASSWORD_KEYS))}@{host}/?retryWrites=true&w=majority"  # URI של Atlas
    if not uri:  # לא נמצאו פרטי חיבור
        where = found.name if found else "no atlas-credentials.env/.env file"  # מקור ההגדרות
        return None, f"no MongoDB connection string found ({where}; keys present: {sorted(values) or 'none'})"  # רק שמות מפתחות, לא ערכים
    return {"uri": uri, "db": first(values, DB_KEYS) or "rag_session4", "collection": first(values, COLLECTION_KEYS) or "chunks",  # הגדרות סופיות
            "vector_index": first(values, INDEX_KEYS) or "vector_index", "source": found.name if found else "environment"}, None  # ומקורן


def sync_index(data, settings, wait_seconds=180):  # מעלים index שלם ל־Atlas ומוודאים Vector Index
    manifest, records = data["manifest"], data["records"]  # מאפייני המודל והקטעים
    if manifest["embedding_mode"] == "offline_fixture":  # מספרי fixture אינם embeddings
        raise ValueError("Refusing to upload an offline_fixture index; build a real one with Ollama")  # לא מעלים נתוני בדיקה
    from pymongo import ReplaceOne  # פעולת upsert
    client = connect(settings["uri"])  # חיבור
    try:  # סוגרים את החיבור בסוף
        db = client[settings["db"]]  # מסד הנתונים
        collection = db[settings["collection"]]  # האוסף
        notes = [r for r in records if r.get("kind") != "chat"]  # note / document chunks
        chats = [r for r in records if r.get("kind") == "chat"]  # chat chunks: their own schema and key (chat_store)
        docs = [{"_id": r["chunk_id"], **r} for r in notes]  # chunk_id הוא גם _id
        upserted = modified = 0
        if docs:  # bulk_write refuses an empty list
            result = collection.bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs])  # כתיבה/עדכון של כל הקטעים
            upserted, modified = result.upserted_count, result.modified_count
        removed = collection.delete_many({"kind": {"$ne": "chat"}, "_id": {"$nin": [d["_id"] for d in docs]}}).deleted_count  # קטעים שאינם עוד ב־index
        db["index_manifests"].replace_one({"_id": settings["collection"]}, {"_id": settings["collection"], **manifest}, upsert=True)  # ה־manifest לחיפוש
        print(f"ATLAS: db={settings['db']} collection={settings['collection']} upserted={upserted} "  # דיווח
              f"updated={modified} removed={removed} total={collection.count_documents({})}")  # ספירה מהשרת עצמו
        import chat_store  # the chats in the index replace the chats in the collection
        chat = chat_store.sync(db, collection, [chat_store.mongo_doc(r, manifest) for r in chats], all_files=True)
        print(f"ATLAS CHATS: inserted={chat['inserted']} updated={chat['updated']} removed={chat['removed']} "
              f"chunks={chat['chunks']} participants={chat['participants']}")
        ensure_vector_index(collection, settings["vector_index"], manifest["dimensions"], wait_seconds)  # אינדקס הווקטורים
    finally:  # גם במקרה של שגיאה
        client.close()  # סוגרים חיבור


def safe_error(error, settings):  # הודעת שגיאה בלי סיסמה או URI
    message = str(error)  # טקסט השגיאה
    if settings and settings.get("uri"):  # מסתירים את ה־URI אם הופיע
        message = message.replace(settings["uri"], "<MONGODB_URI>")  # הסתרה
    return f"{type(error).__name__}: {message}"  # הודעה בטוחה


def main():  # נקודת ההפעלה: העלאה ידנית של index קיים
    parser = argparse.ArgumentParser(description="Upload exercise-1 index to MongoDB Atlas Vector Search")  # עזרה
    parser.add_argument("--index", default="build/index.json")  # ה־index שנבנה בשלב embed
    parser.add_argument("--env-file", help="Credentials file (default: atlas-credentials.env or .env next to this script)")  # קובץ הגדרות
    parser.add_argument("--wait", type=int, default=180, help="Seconds to wait for the vector index to become queryable")  # זמן המתנה
    args = parser.parse_args()  # קוראים אפשרויות
    settings = None  # יוגדר אחרי קריאת הקובץ
    try:  # טיפול מרוכז בשגיאות צפויות
        settings, problem = atlas_settings(Path(__file__).resolve().parent, args.env_file)  # פרטי החיבור
        if problem:  # אין פרטי חיבור
            raise ValueError(problem)  # הודעה עם שמות המפתחות בלבד
        data = json.loads(Path(args.index).read_text(encoding="utf-8"))  # קוראים את ה־index
        if data.get("stage") != "index":  # רק תוצר של שלב embed
            raise ValueError("Use build/index.json from the embed stage")  # הנחיה
        sync_index(data, settings, args.wait)  # העלאה
    except Exception as error:  # כולל שגיאות רשת/הרשאה של pymongo
        print(f"STOP: {safe_error(error, settings)}", file=sys.stderr)  # הודעת שגיאה בלי סיסמה
        return 2  # קוד כשל
    return 0  # הצלחה


if __name__ == "__main__":  # הרצה ישירה
    raise SystemExit(main())  # קוד יציאה
