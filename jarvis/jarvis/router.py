"""Router: one input box for everything. Decides which skill a request belongs to.

  route("find me 5 youtube shorts about cooking")  -> shorts, search "cooking", limit 5
  route("מה הוחלט בישיבת הצוות?")                    -> rag (questions about the notes are the default)
  route("@MrBeast")                                -> shorts, latest Shorts of the channel
  route("https://youtube.com/shorts/abc123")       -> shorts, details of that one Short
  route("what does the team like to eat? find shorts about it")
  route("תמצא לי שורטס על מה שהצוות אוהב לאכול") -> combo: answer from the notes, then Shorts built from the answer
  route("make a meme about monday meetings")       -> meme (the memes MCP server; "תעשה מם על..." too)
  route("tell me a joke about programmers")         -> meme too: a joke is answered with a meme ("תספר לי בדיחה")
  route("make a song about monday meetings")       -> song (the music MCP server; "תעשה לי שיר על..." too)
  route("system check") / route("בדיקת מערכות")      -> status
  route("/shorts cats") / route("/docs ...")         -> force a skill

Two ways to route (the "Route with" option in Ask Jarvis; JARVIS_ROUTER sets the default):
  rules  (default) - route(): plain patterns only. Instant, offline, predictable; a new skill means new patterns.
  model            - only the sure cases use patterns (sure_route: /commands, YouTube links, a bare @handle,
                     "system check", "sync the vault"). Everything else goes to the answer model with the list of
                     TOOLS: it picks one tool and its input (tool_prompt -> from_tool). If the model fails, the rules decide.
                     gemma3:4b on a CPU: ~17 s and ~70% right on the test phrases, so it is not the default.

With the rules, anything not clearly about YouTube or the system goes to the documents, so a question about
the notes is never sent to YouTube by accident. The model gets the same rule in its prompt.
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
REFERENCE = re.compile(r"\b(?:about|for|of|on)\s+(?:it|that|this|them|those|these)\b|\bbetween\s+them\b|"
                       r"\b(?:this|that|these|those)\s+(?:situation|info|information|story|thing|answer|relationship)s?\b|"
                       r"(?:^|\s)(?:ביניהם|ביניהן|בינהם|בינהן|שלהם|שלהן)(?=[\s?!.,]|$)|"
                       r"(?:^|\s)\S+\s+(?:הזה|הזאת|הזו|האלה|האלו)(?=[\s?!.,]|$)|"
                       r"(?:^|\s)(?:על\s+(?:זה|זאת|זו|הנושא\s+הזה|הדבר\s+הזה)|עליו|עליה|עליהם|עליהן|לזה|מזה)(?=[\s?!.,]|$)", re.I)
NOTES = re.compile(r"\b(?:team|teams|team's|our|meetings?|projects?|notes?|vault|obsidian|decided|agreed|summary)\b|"
                   r"צוות|ישיב|פרויקט|פרוייקט|שלנו|בפתקים|בהערות|סוכם|הוחלט|סיכום", re.I)
# "question? find shorts about it" / "... and show me videos" -> the notes part and the YouTube part
CLAUSES = re.compile(r"[?!.\n]+|\s+(?:and\s+then|then|and)\s+(?=(?:find|show|search|get|give|bring|play|make)\b)|"
                     r"\s+(?:ו?אז|ואחר\s+כך|אחר\s+כך|ואחרי\s+זה|אחרי\s+זה)\s+|"
                     r"\s+ו(?=(?:תמצא|תחפש|תראה|תביא|תן|מצא|חפש|הראה|תציג)(?:\s|$))", re.I)
PRONOUN = re.compile(r"(?:^|\s)(?:it|that|this|them|those|these|זה|זאת|זו|הזה|הזאת|אותו|אותה|אותם)(?=\s|$)", re.I)
FORCE = {"docs": "rag", "rag": "rag", "ask": "rag", "shorts": "shorts", "youtube": "shorts", "yt": "shorts",
         "meme": "meme", "memes": "meme", "song": "song", "songs": "song", "music": "song",
         "status": "status", "sync": "vault_sync"}
# "make a meme about ..." / "תעשה לי מם על ...": the meme word plus a make verb (a question about memes stays with the notes)
MEME = re.compile(r"\bmemes?\b|(?:^|\s)(?:ה?מם|ה?מימ|ה?ממים|ה?מימים|מימז)(?=[\s?!.,:]|$)", re.I)
# "תיצור לימים על..." (= "לי מימים", typed together): only with a make verb, since "לימים" alone means "later on"
MEME_JOINED = re.compile(r"(?:^|\s)(?:לי|ל)(?:מם|מימ|ממים|מימים|מימז|מים)(?=[\s?!.,:]|$)")
# Speech-to-text also writes "מם" as a separate "מים" (water): a meme only with a make verb and before "על / של / עם"
# ("תעשה לי מים על בצלאל"), so "תביא לי מים" stays water.
MEME_HEARD = re.compile(r"(?:^|\s)(?:מימס|ממז)(?=[\s?!.,:]|$)|(?:^|\s)ה?מים\s+(?:על|של|עם|בנושא)(?=\s|$)")
MAKE = re.compile(r"\b(?:make|create|generate|build|draw|do|give|send|write)\b|"
                  r"(?:^|\s)(?:תעשה|תעשי|תכין|תכיני|צור|תיצור|תייצר|עשה|הכן|תן|תני|תביא|תמציא|תכתוב)(?=\s|$)", re.I)
# "make a song about ..." / "תעשה לי שיר על ...": a song word plus a make verb. "שיר" is also a name (Shir), so never
# without the verb; a question about a song ("what song did we pick?", "מה השיר של הצוות?") stays with the notes.
SONG = re.compile(r"\b(?:songs?|tracks?|tunes?|jingles?|anthems?|ballads?|lullaby|lullabies|raps?|music)\b|"
                  r"(?:^|\s)(?:ה?שיר|ה?שירים|ה?שירון|ג['׳]ינגל|ה?המנון|ה?ראפ|ה?מוזיקה|ה?מנגינה|ה?בלדה)(?=[\s?!.,:]|$)", re.I)
SONG_MAKE = re.compile(r"\b(?:compose|sing|produce|record)\b|(?:^|\s)(?:תלחין|תלחיני|תשיר|תשירי|תחבר|תחברי|תפיק)(?=\s|$)", re.I)
# The request is the song itself: "song about cats", "i want a song about cats", "play me a jingle for the team",
# "שיר על חתולים", "אני רוצה שיר על הצוות", "בא לי שיר על הים". The wish has to be right before the song word, so
# "Shir wants to talk" ("שיר רוצה לדבר") and "i like that song" stay with the notes.
_SONG_WORD = r"(?:songs?|jingles?|anthems?|ballads?|lullaby|raps?|tunes?)"
_SHIR_WORD = r"(?:שיר|ג['׳]ינגל|המנון|ראפ|בלדה)"
SONG_ABOUT = re.compile(rf"^\s*(?:(?:i\s+)?(?:want|need|would\s+like|'d\s+like|play)\s+(?:me\s+)?)?(?:an?\s+|some\s+)?"
                        rf"{_SONG_WORD}\s+(?:about|for|on)\b|"
                        rf"^\s*(?:(?:אני\s+)?(?:רוצה|צריך|צריכה|תשמיע|תשמיעי|בא)\s+(?:לי\s+)?)?{_SHIR_WORD}\s+"
                        rf"(?:על|בנושא|עבור|ל(?=\S))", re.I)
# A joke request is answered with a meme: "tell me a joke", "make me laugh", "תספר לי בדיחה", "תצחיק אותי".
# A question about a joke ("what joke did Idan tell at the meeting?") stays with the notes.
JOKE = re.compile(r"\bjokes?\b|\bmake\s+(?:me|us)\s+laugh\b|\bsomething\s+funny\b|\bcheer\s+me\s+up\b|"
                  r"(?:^|\s)(?:ה?בדיחה|בדיחות|ה?בדיחות|תצחיק|תצחיקי|משהו\s+מצחיק|תשמח\s+אותי)(?=[\s?!.,:]|$)", re.I)
TELL = re.compile(r"\btell\s+(?:me|us|an?)\b|\b(?:know|got|have)\s+(?:any|an?)\b|\bsay\s+an?\b|"
                  r"(?:^|\s)(?:ספר|תספר|תספרי|תגיד|תגידי|יש\s+לך|תצחיק|תצחיקי|תשמח)(?=\s|$)", re.I)
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
    skill: str  # rag | shorts | combo | meme | song | status | vault_sync
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
    if REFERENCE.search(text):  # "a video that shows the relationship between them": about the previous answer
        return ""
    topic = PRONOUN.sub(" ", REFERENCE.sub(" ", shorts_query(text))).strip()  # "find shorts about the team's food" -> "team's food"
    if topic.startswith("ש"):  # "על מה שהצוות אוהב" -> "מה שהצוות אוהב"
        topic = "מה " + topic
    return topic


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
    if (MEME.search(text) and (MAKE.search(text) or not (QUESTION.match(text) or STRONG.search(text)))) or \
            ((MEME_JOINED.search(text) or MEME_HEARD.search(text)) and MAKE.search(text)):
        return Route("meme", text, {}, "meme request → the memes MCP server")
    if ((SONG.search(text) and (MAKE.search(text) or SONG_MAKE.search(text)))
            or (SONG_ABOUT.search(text) and not text.rstrip().endswith("?"))) \
            and not QUESTION.match(text) and not STRONG.search(text):
        return Route("song", text, {}, "song request → the music MCP server")
    if JOKE.search(text) and not STRONG.search(text) and (MAKE.search(text) or TELL.search(text) or not QUESTION.match(text)):
        return Route("meme", text, {}, "joke request → a meme from the memes MCP server")
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
        if not question:  # "find shorts about it": about the previous answer in this conversation
            return Route("combo", "", {"mode": "search", "follow_up": True, **extra},
                         "YouTube about the previous answer → search Shorts about it")
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


# -- model routing -----------------------------------------------------------------------------------------------
def sure_route(text):  # requests the patterns can't get wrong; None = let the model decide
    text = (text or "").strip()
    forced = re.match(r"^/(\w+)", text)
    if (forced and forced.group(1).lower() in FORCE) or STATUS.match(text) or SYNC.match(text) \
            or YOUTUBE_URL.search(text) or HANDLE.fullmatch(text):
        return route(text)
    return None


# The tools the model can pick from. "server"/"tool" name the MCP tool behind a choice: describe() can replace the
# description with the live one from the server's list_tools(), so the model reads what the server itself says.
TOOLS = [
    {"name": "ask_notes", "input": "the request, as asked",
     "description": "Answer from the user's notes and documents. The default, for any question."},
    {"name": "search_youtube_shorts", "server": "youtube-shorts", "tool": "search_youtube_shorts",
     "input": "the search words only",
     "description": "Search YouTube Shorts by keyword or hashtag."},
    {"name": "get_youtube_shorts", "server": "youtube-shorts", "tool": "get_youtube_shorts",
     "input": "the channel, e.g. @MrBeast",
     "description": "Get the latest YouTube Shorts of a channel."},
    {"name": "notes_then_shorts", "input": "the question for the notes, or empty for the previous answer",
     "description": "Answer from the notes, then search YouTube Shorts about the answer."},
    {"name": "make_meme", "server": "memes", "tool": "generate_meme", "input": "what the meme is about",
     "description": "Make a meme. Also for jokes ('tell me a joke', 'תצחיק אותי')."},
    {"name": "make_song", "server": "music", "tool": "create_song", "input": "what the song is about",
     "description": "Make a song (music with vocals) about something."},
    {"name": "system_check", "input": "empty", "description": "Check Jarvis's systems."},
    {"name": "sync_vault", "input": "empty", "description": "Re-index the Obsidian vault now."},
]
TOOL_NAMES = [t["name"] for t in TOOLS]
TOOL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["tool", "input"],
               "properties": {"tool": {"type": "string", "enum": TOOL_NAMES}, "input": {"type": "string"}}}
TOOL_PROMPT = """Pick the ONE tool for the request to Jarvis, and its input.

{tools}

Rules: a question is ask_notes, even if it mentions YouTube, videos, memes or jokes ("who owns the youtube project?",
"מה הוחלט על המם?"). Videos about the team, a person or "it / that" -> notes_then_shorts. "מים" is water, a meme only
after a make verb ("תעשה לי מים על..."). "שיר" is a song (or the name Shir): make_song only after a make verb.
Keep the request's language.

find me 5 youtube shorts about cooking -> {{"tool": "search_youtube_shorts", "input": "cooking"}}
מה הצוות אוהב לאכול? תמצא לי שורטס על זה -> {{"tool": "notes_then_shorts", "input": "מה הצוות אוהב לאכול?"}}
תספר לי בדיחה על מתכנתים -> {{"tool": "make_meme", "input": "מתכנתים"}}
תכתוב לי שיר על הצוות -> {{"tool": "make_song", "input": "הצוות"}}
מה הוחלט בישיבת הצוות? -> {{"tool": "ask_notes", "input": "מה הוחלט בישיבת הצוות?"}}

{conversation}Request: {request}
JSON:"""


def describe(tool, live=None):  # live(server, tool) -> description from the MCP server, or None
    text = live(tool["server"], tool["tool"]) if live and tool.get("server") else None
    text = " ".join(str(text or tool["description"]).split())
    return re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0][:200]  # what it does; its parameters don't help to choose


def tool_prompt(text, history=(), live=None):
    tools = "\n".join(f"- {t['name']}(input: {t['input']}): {describe(t, live)}" for t in TOOLS)
    last = list(history)[-1:]  # the previous turn, so "about it" has something to point at
    conversation = "".join(f"Previous question: {t['question'][:300]}\nPrevious answer: {t['answer'][:300]}\n\n" for t in last)
    return TOOL_PROMPT.format(tools=tools, conversation=conversation, request=text)


def from_tool(name, given, text, by="model"):  # the model's choice -> the same Route the rules would build
    given = " ".join(str(given or "").split())[:200]
    limit = count(text)
    extra = {"limit": limit} if limit else {}
    why = f"{by} → {name}" + (f" “{given}”" if given else "")
    if name == "ask_notes":
        return Route("rag", text, {}, why)  # as asked: a follow-up is rewritten by the documents skill itself
    if name == "search_youtube_shorts":
        query = shorts_query(given) or shorts_query(text)
        return Route("shorts", query, {"mode": "search", **extra}, why)
    if name == "get_youtube_shorts":
        channel = given.split()[0] if given else ""
        mode = "channel" if channel else "search"
        return Route("shorts", channel or shorts_query(text), {"mode": mode, **extra}, why)
    if name == "notes_then_shorts":
        if not given or PRONOUN.sub(" ", given).strip(" ?!.,") == "":
            return Route("combo", "", {"mode": "search", "follow_up": True, **extra}, why)
        return Route("combo", given, {"mode": "search", **extra}, why)
    if name == "make_meme":
        return Route("meme", text, {}, why)  # as asked: the memes skill reads templates and captions from the words
    if name == "make_song":
        return Route("song", text, {}, why)  # as asked: the songs skill reads style, lyrics and title from the words
    if name == "system_check":
        return Route("status", "", {}, why)
    if name == "sync_vault":
        return Route("vault_sync", "", {}, why)
    raise ValueError(f"unknown tool {name!r}")
