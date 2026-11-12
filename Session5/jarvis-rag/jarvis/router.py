"""Router: one input box for everything. Decides which skill a request belongs to.

  route("find me 5 youtube shorts about cooking")  -> shorts, search "cooking", limit 5
  route("מה הוחלט בישיבת הצוות?")                    -> rag (questions about the notes are the default)
  route("@MrBeast")                                -> shorts, latest Shorts of the channel
  route("https://youtube.com/shorts/abc123")       -> shorts, details of that one Short
  route("what does the team like to eat? find shorts about it")
  route("תמצא לי שורטס על מה שהצוות אוהב לאכול") -> combo: answer from the notes, then Shorts built from the answer
  route("system check") / route("בדיקת מערכות")      -> status
  route("/shorts cats") / route("/docs ...")         -> force a skill

The rules are plain patterns on purpose: instant, offline, predictable, and the console can show
the right pipeline stages before the run starts. Anything not clearly about YouTube or the system
goes to the documents, so a question about the notes is never sent to YouTube by accident.
"""
import re
from dataclasses import dataclass, field

YOUTUBE_URL = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)[\w\-?=&%.]+", re.I)
HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9][\w.\-]{1,60}")
# Words that alone mean YouTube ("shorts", "youtube"); and weaker ones ("videos") that need a "find/show" verb too.
STRONG = re.compile(r"\b(?:youtube|yt|shorts?)\b|יוטיוב|שורטס|שורטים|שורט\b", re.I)
WEAK = re.compile(r"\b(?:videos?|clips?|reels?)\b|סרטונים|סרטון|קליפים|קליפ", re.I)
VERB = re.compile(r"\b(?:find|show|search|get|fetch|look\s+up|give|bring|list|play)\b|"
                  r"(?:^|\s)ו?(?:ת?מצא|חפש|תחפש|תראה|הראה|הצג|תציג|תביא|הבא|תן|תני|תמצאי|תחפשי|תראי)(?=\s|$)", re.I)
CHANNEL_WORD = re.compile(r"\b(?:from\s+(?:the\s+)?channel|channel|from)\s+([^\s?!.,]+)|(?:^|\s)(?:ערוץ|מהערוץ|בערוץ)\s+([^\s?!.,]+)", re.I)
SHORTS_WORD = re.compile(r"\bshorts?\b|שורטס|שורטים|שורט\b", re.I)
# A question about the notes that happens to mention YouTube ("who owns the youtube project?") stays with the documents.
QUESTION = re.compile(r"^\s*(?:who|what|when|where|why|how|which|whose|did|does|do|is|are|was|were|should|"
                      r"מי|מה|מתי|איפה|למה|מדוע|איך|האם|כמה|איזה|איזו|אילו)\b", re.I)
COUNT = re.compile(r"(?<![\w.])(\d{1,2})(?![\w.])")
STATUS = re.compile(r"^\s*(?:run\s+(?:a\s+)?)?(?:system\s+(?:check|status)|status|health\s*check|check\s+(?:the\s+)?systems?)\s*[?.!]*\s*$|"
                    r"^\s*(?:תריץ\s+|תעשה\s+)?(?:בדיקת\s+מערכות|מצב\s+(?:ה)?מערכות?|סטטוס)\s*[?.!]*\s*$", re.I)
SYNC = re.compile(r"^\s*(?:(?:re-?)?sync|re-?index)\s+(?:the\s+)?(?:vault|notes|obsidian)\s*[?.!]*\s*$|"
                  r"^\s*(?:סנכרן|תסנכרן|אנדקס\s+מחדש)\s+(?:את\s+)?(?:ה)?(?:כספת|פתקים|ווליט|vault|obsidian)\s*[?.!]*\s*$", re.I)
# A YouTube request whose topic lives in the notes: it points back ("about it") or names something from the vault.
REFERENCE = re.compile(r"\b(?:about|for|of|on)\s+(?:it|that|this|them|those|these)\b|"
                       r"(?:^|\s)(?:על\s+(?:זה|זאת|זו|הנושא\s+הזה|הדבר\s+הזה)|עליו|עליה|עליהם|עליהן|לזה|מזה)(?=[\s?!.,]|$)", re.I)
NOTES = re.compile(r"\b(?:team|teams|team's|our|meetings?|projects?|notes?|vault|obsidian|decided|agreed|summary)\b|"
                   r"צוות|ישיב|פרויקט|פרוייקט|שלנו|בפתקים|בהערות|סוכם|הוחלט|סיכום", re.I)
# "question? find shorts about it" / "... and show me videos" -> the notes part and the YouTube part
CLAUSES = re.compile(r"[?!.\n]+|\s+(?:and|then)\s+(?=(?:find|show|search|get|give|bring|play)\b)|"
                     r"\s+ו(?=(?:תמצא|תחפש|תראה|תביא|תן|מצא|חפש|הראה|תציג)(?:\s|$))", re.I)
FORCE = {"docs": "rag", "rag": "rag", "ask": "rag", "shorts": "shorts", "youtube": "shorts", "yt": "shorts",
         "status": "status", "sync": "vault_sync"}
# Filler around a Shorts query: "find me some youtube shorts about cooking please" -> "cooking"
FILLER = re.compile(
    r"\b(?:please|can|could|would|you|me|us|some|a|an|the|few|couple|of|about|on|for|with|related|to|regarding|"
    r"latest|recent|new|newest|top|best|popular|trending|i|want|need|like|to\s+see|watch|any|from|channel|"
    r"youtube|yt|shorts?|videos?|clips?|reels?|find|show|search|get|fetch|look|up|give|bring|list|play)\b|"
    r"(?:^|\s)(?:בבקשה|אפשר|לי|לנו|כמה|על|בנושא|של|עם|קשורים|קשור|ל|את|מה|האחרונים|אחרונים|חדשים|הכי|טובים|"
    r"פופולריים|רוצה|לראות|יוטיוב|ביוטיוב|מיוטיוב|שורטס|שורטים|שורט|סרטונים|סרטון|קליפים|קליפ|ת?מצא|חפש|תחפש|"
    r"תראה|הראה|הצג|תציג|תביא|הבא|תן|תני|תמצאי|תחפשי|תראי|ערוץ|מהערוץ|בערוץ)(?=\s|$)", re.I)


@dataclass
class Route:
    skill: str  # rag | shorts | combo | status | vault_sync
    question: str = ""  # what the target skill receives
    options: dict = field(default_factory=dict)  # extra request values, e.g. {"mode": "channel", "limit": 5}
    reason: str = ""  # shown in the console: why this skill was chosen


def shorts_query(text):
    words = FILLER.sub(" ", COUNT.sub(" ", text))
    return re.sub(r"\s+", " ", re.sub(r"[?!.,:;\"“”'׳]+", " ", words)).strip()


def notes_question(text):  # the part of a combo request that the notes answer
    parts = [p.strip() for p in CLAUSES.split(text) if p and p.strip()]
    asked = [p for p in parts if not (STRONG.search(p) or WEAK.search(p))]
    if asked:
        return " ".join(asked)
    topic = REFERENCE.sub(" ", shorts_query(text)).strip()  # "find shorts about the team's favorite food" -> "team's favorite food"
    if topic.startswith("ש"):  # "על מה שהצוות אוהב" -> "מה שהצוות אוהב"
        topic = "מה " + topic
    return topic or text


def count(text):
    found = COUNT.search(text)
    return int(found.group(1)) if found and 1 <= int(found.group(1)) <= 25 else None


def route(text):
    text = (text or "").strip()
    forced = re.match(r"^/(\w+)\s*(.*)$", text, re.S)
    if forced and forced.group(1).lower() in FORCE:
        skill, rest = FORCE[forced.group(1).lower()], forced.group(2).strip()
        if skill == "shorts":
            inner = route(rest)
            if inner.skill == "shorts":
                inner.reason = "/" + forced.group(1) + " · " + inner.reason
                return inner
            return Route("shorts", rest, {"mode": "search"}, f"/{forced.group(1)} → search Shorts")
        return Route(skill, rest, {}, f"/{forced.group(1)} → {skill}")
    if STATUS.match(text):
        return Route("status", "", {}, "system check request")
    if SYNC.match(text):
        return Route("vault_sync", "", {}, "vault sync request")
    url = YOUTUBE_URL.search(text)
    if url:
        return Route("shorts", url.group(0), {"mode": "url"}, "YouTube link → details of that video")
    limit = count(text)
    extra = {"limit": limit} if limit else {}
    handle = HANDLE.search(text)
    strong, weak = STRONG.search(text), WEAK.search(text) and VERB.search(text)
    if QUESTION.match(text) and not handle and not SHORTS_WORD.search(text) and not VERB.search(text):
        strong = weak = None
    if handle and (strong or weak or len(shorts_query(text.replace(handle.group(0), ""))) == 0):
        return Route("shorts", handle.group(0), {"mode": "channel", **extra}, f"channel {handle.group(0)} → its latest Shorts")
    if (strong or weak) and not handle and (REFERENCE.search(text) or NOTES.search(text)):
        question = notes_question(text)
        return Route("combo", question, {"mode": "search", **extra},
                     "notes + YouTube → answer from your notes, then search Shorts about the answer")
    if strong or weak:
        channel = CHANNEL_WORD.search(text)
        name = channel and (channel.group(1) or channel.group(2))
        if name and not FILLER.fullmatch(" " + name):
            return Route("shorts", name, {"mode": "channel", **extra}, f"channel {name} → its latest Shorts")
        query = shorts_query(text)
        if query:
            return Route("shorts", query, {"mode": "search", **extra}, f"YouTube request → search Shorts for “{query}”")
        return Route("shorts", "", {"mode": "search", **extra}, "YouTube request without a topic")
    return Route("rag", text, {}, "question → your documents")
