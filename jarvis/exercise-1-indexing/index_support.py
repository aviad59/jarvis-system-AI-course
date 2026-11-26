"""חלקים מסופקים: קריאת מסמכים, אימות נתונים וקריאה ל־Ollama.

אין כאן חיפוש או יצירת תשובה. הפונקציות תומכות בהכנת index מקומי בקובץ JSON.
"""
import hashlib  # טוענים כלי עבור זיהוי תוכן באמצעות hash
import json  # טוענים כלי עבור קריאה וכתיבה של קבצי התוצרים
import math  # טוענים כלי עבור חישוב אורכי וקטורים ובדיקת מספרים סופיים
import re  # טוענים כלי עבור פיצול טקסט וקריאת שדות המקור
import os  # JARVIS_VAULT_INCLUDE
import time  # תאריך עדכון של פתק לפי זמן שינוי הקובץ
import urllib.error  # טוענים כלי עבור זיהוי שגיאת חיבור או API
import urllib.request  # טוענים כלי עבור שליחת בקשות HTTP לספק embeddings
import sys  # find chat_parser next to this file when loaded by path (Jarvis)
from pathlib import Path  # משתמשים בנתיבים שמאפשרים לפתוח ולשמור את קבצי התרגיל

BASE = Path(__file__).resolve().parent  # שומרים את BASE עבור המשך הפעולה
if str(BASE) not in sys.path:
    sys.path.append(str(BASE))  # appended: never shadows another folder's modules
from chat_parser import is_chat_export  # noqa: E402  WhatsApp exports in the vault get message-aligned chunks
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


# ---------------------------------------------------------------------------
# Obsidian vault (session 5): team notes as a document source
# ---------------------------------------------------------------------------
# Default: the whole vault, so a note in any folder (a new one too) is found. To narrow it, list folders:
# JARVIS_VAULT_INCLUDE=Projects,Meetings  (the whole vault is used if none of them exist)
VAULT_INCLUDE = tuple(n.strip() for n in os.environ.get("JARVIS_VAULT_INCLUDE", "").split(",") if n.strip())
VAULT_EXCLUDE_DIRS = {".obsidian", ".trash", ".git", "Templates", "Drafts"}  # never indexed, even when scanning the whole vault
VAULT_EXCLUDE_FILES = {"indexing-scope.md"}  # describes the scope; it is not team knowledge
VAULT_PREFIX = "vault/"  # source = "vault/<path inside the vault>", so vault chunks are easy to tell apart


def _frontmatter(text):  # (properties dict, body) for a note that may start with Obsidian Properties (--- YAML ---)
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    after = text.find("\n", end + 4)
    body = "" if after == -1 else text[after + 1:]
    props, key = {}, None
    for line in text[4:end].split("\n"):  # small YAML subset: "key: value" and "- item" lists
        item = re.match(r"^\s+-\s*(.*)$", line)
        if item and key:
            props[key] = (props[key] + ", " if props[key] else "") + item.group(1).strip().strip("'\"")
            continue
        pair = re.match(r"^([A-Za-z_][\w -]*):\s*(.*)$", line)
        if pair:
            key = pair.group(1).strip()
            value = pair.group(2).strip()
            props[key] = value[1:-1] if len(value) > 1 and value[0] == "[" and value[-1] == "]" else value.strip("'\"")
    return props, body


def vault_files(vault, include=VAULT_INCLUDE):  # (files, scope description); falls back to the whole vault if no include folder exists
    vault = Path(vault)
    if not vault.is_dir():
        raise ValueError(f"Vault folder not found: {vault}")
    folders = [vault / name for name in include if (vault / name).is_dir()] if include else []
    roots, scope = (folders, ", ".join(f.name for f in folders)) if folders else ([vault], "whole vault")
    files = []
    for root in roots:
        for path in sorted(root.rglob("*.md")):
            relative = path.relative_to(vault)
            if VAULT_EXCLUDE_DIRS.intersection(relative.parts[:-1]) or path.name in VAULT_EXCLUDE_FILES:
                continue
            files.append(path)
    return files, scope


def vault_snapshot(vault, include=VAULT_INCLUDE):  # cheap change detector: (path, mtime, size) of every note in scope
    try:
        files, _ = vault_files(vault, include)
    except ValueError:
        return None
    snapshot = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:  # deleted between listing and stat
            continue
        snapshot.append((path.as_posix(), stat.st_mtime_ns, stat.st_size))
    return tuple(snapshot)


def vault_signature(vault, include=VAULT_INCLUDE):  # {source: sha256} of the notes an index of this vault must contain
    return {d["source"]: d["document_sha256"] for d in _read_vault(Path(vault), vault_files(vault, include)[0], quiet=True)}


def load_vault(vault, include=VAULT_INCLUDE):  # read Obsidian notes into the same document dicts as load_documents
    vault = Path(vault)
    files, scope = vault_files(vault, include)
    print(f"VAULT: {vault} · scope: {scope} · {len(files)} notes")
    documents = _read_vault(vault, files)
    if not documents:
        raise ValueError(f"No non-empty .md notes found in the vault ({scope}): {vault}")
    return documents


def _read_vault(vault, files, quiet=False):
    documents = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig")  # universal newlines: CRLF becomes \n, so paragraph splitting works
        except OSError:  # deleted or locked while Obsidian saves it
            continue
        props, body = _frontmatter(text)
        relative = path.relative_to(vault).as_posix()
        if not body.strip():
            if not quiet:
                print(f"VAULT SKIP (empty note): {relative}")
            continue
        heading = re.search(r"^# (.+)$", body, re.M)
        meeting_date = props.get("meeting_date", "")
        updated = (props.get("updated") or meeting_date or props.get("date")
                   or time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime)))
        document = {
            "document_id": props.get("document_id") or relative.removesuffix(".md"),
            "version": str(props.get("version") or "1"),
            "status": props.get("status") or "current",
            "updated": updated,
            "source": VAULT_PREFIX + relative,
            "title": props.get("title") or (heading.group(1).strip() if heading else path.stem),
            "body": body, "document_sha256": digest(text),
            "path": relative,  # the note's path inside the vault (open it in Obsidian)
        }
        for field in ("meeting_date", "project_id"):  # kept with every chunk when the note has them
            if props.get(field):
                document[field] = props[field]
        if is_chat_export(body):  # a WhatsApp export: chunked by message (chat_parser.py), not by paragraph
            document["kind"] = "chat"
        documents.append(document)
    return documents


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
