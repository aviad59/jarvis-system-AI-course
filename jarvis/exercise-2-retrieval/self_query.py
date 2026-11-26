"""Self-query: a natural-language question -> {query, senders, from, to} for filtered chat retrieval.

One structured-output call to the answer model (any provider; the caller passes `ask(prompt, schema)`)
extracts the filters; this module builds the prompt and checks the reply:
  - senders must map to a known participant (exact, first/last name, same sound across Hebrew and
    English, or a close spelling) and must actually be named in the question; others are dropped
  - from/to must be valid dates; relative dates are resolved from a calendar placed in the prompt
  - query keeps only the topic; when no filter survives, the question is searched exactly as asked
"""
import difflib  # close spellings
import re  # tokens
import unicodedata  # name normalization
from datetime import date, datetime, timedelta

SCHEMA = {"type": "object", "properties": {
    "query": {"type": "string"},
    "senders": {"type": "array", "items": {"type": "string"}},
    "from": {"type": "string"}, "to": {"type": "string"}},
    "required": ["query", "senders", "from", "to"], "additionalProperties": False}

PROMPT = """You turn a question about a group chat into a search request. Reply with JSON only.

Fields:
- query: only what the question is about (its topic), in the question's language. Remove the people's names
  and every time expression. If nothing is left (for example "what was said yesterday?"), use "{generic}".
- senders: the people whose messages the question asks about, written exactly as in the participants list.
  Only people the question names; [] if it names nobody. A name in another script or a first name alone still
  maps to the list (for example עומר -> Omer, ירדן -> Yarden).
- from, to: the dates the question asks about, as YYYY-MM-DD (YYYY-MM-DDTHH:MM if it gives a time of day);
  "" when it gives no time. One day: from and to are the same day. Copy relative dates from the calendar.
  A date without a year is the latest such date that is not after today.

Participants: {participants}

Calendar:
{calendar}

Examples:
{examples}

Question: {question}"""

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def calendar(today):  # relative dates, computed here so a small model only has to copy them
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=(today.weekday() + 1) % 7)  # weeks start on Sunday (Israel)
    last_week_start, last_week_end = week_start - timedelta(days=7), week_start - timedelta(days=1)
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    rows = [("today / היום", today, today), ("yesterday / אתמול", yesterday, yesterday),
            ("day before yesterday / שלשום", today - timedelta(days=2), today - timedelta(days=2)),
            ("this week / השבוע", week_start, today), ("last week / בשבוע שעבר", last_week_start, last_week_end),
            ("last 7 days / בשבוע האחרון", today - timedelta(days=6), today),
            ("this month / החודש", month_start, today), ("last month / בחודש שעבר", last_month_end.replace(day=1), last_month_end)]
    lines = [f"today is {WEEKDAYS[today.weekday()]} {today.isoformat()}"]
    lines += [f"{label}: from {a.isoformat()} to {b.isoformat()}" for label, a, b in rows]
    return "\n".join(lines), {label.split(" / ")[0]: (a, b) for label, a, b in rows}


def latest(month, day, today):  # "5 August" without a year -> the latest such date not after today
    year = today.year if (month, day) <= (today.month, today.day) else today.year - 1
    return date(year, month, day)


def build_prompt(question, participants, today):
    text, ranges = calendar(today)
    person = participants[0] if participants else "Dana Cohen"
    first = person.split()[0]
    a, b = ranges["last week"]
    aug5 = latest(8, 5, today).isoformat()
    y = ranges["yesterday"][0].isoformat()
    examples = "\n".join([
        f'Question: what did {first} say about the trip last week? -> {{"query": "the trip", "senders": ["{person}"], '
        f'"from": "{a}", "to": "{b}"}}',
        f'Question: מה נאמר ב-5 באוגוסט? -> {{"query": "מה דובר", "senders": [], "from": "{aug5}", "to": "{aug5}"}}',
        f'Question: מה דיברו אתמול על המסיבה? -> {{"query": "המסיבה", "senders": [], "from": "{y}", "to": "{y}"}}',
        'Question: what is the plan for the meeting? -> {"query": "what is the plan for the meeting?", "senders": [], '
        '"from": "", "to": ""}'])
    generic = "מה דובר" if re.search(r"[֐-׿]", question) else "what was discussed"
    return PROMPT.format(generic=generic, participants=", ".join(participants) or "(none)", calendar=text,
                         examples=examples, question=question)


# ---------------------------------------------------------------------------
# Names: Hebrew <-> English by sound, first names, close spellings
# ---------------------------------------------------------------------------
HEBREW_SOUNDS = {"ב": "b", "ג": "g", "ד": "d", "ז": "z", "ט": "t", "כ": "k", "ך": "k", "ל": "l", "מ": "m", "ם": "m",
                 "נ": "n", "ן": "n", "ס": "s", "פ": "p", "ף": "p", "צ": "s", "ץ": "s", "ק": "k", "ר": "r", "ש": "s", "ת": "t"}
LATIN_SOUNDS = (("sch", "s"), ("sh", "s"), ("ch", "h"), ("ph", "p"), ("ck", "k"), ("tz", "s"), ("ts", "s"), ("th", "t"),
                ("w", "b"), ("v", "b"), ("f", "p"), ("c", "k"), ("q", "k"), ("x", "ks"), ("j", "g"))
HEBREW_PREFIXES = "והבכלמש"  # "ועומר", "שעומר" -> "עומר"
# Words that never count as a name mention: "אמר" (said) sounds like Omer ("mr"); "של" is inside a contact label
STOP = {"של", "עם", "את", "על", "מה", "מי", "אמר", "אמרה", "אמרו", "כתב", "כתבה", "כתבו", "the", "of", "and", "said", "say"}


def sound_key(word):  # consonants only, the same for both scripts ("ברנשטיין" / "Bernstein" -> "brnstn")
    word = word.lower()
    if re.search(r"[֐-׿]", word):
        key = "".join(HEBREW_SOUNDS.get(c, "") for c in word)
    else:
        for a, b in LATIN_SOUNDS:
            word = word.replace(a, b)
        key = re.sub(r"[aeiouyh\W\d_]", "", word)
    return re.sub(r"(.)\1+", r"\1", key)


def _norm(name):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", name)).strip().casefold()


def _tokens(text):
    return re.findall(r"\w+", _norm(text))


def _forms(word):  # a word and its forms without up to two Hebrew prefix letters
    forms = {word}
    for cut in (1, 2):
        if len(word) - cut >= 2 and all(c in HEBREW_PREFIXES for c in word[:cut]):
            forms.add(word[cut:])
    return forms


def _same(a, b):  # two name tokens are the same name (spelling or sound)
    if a == b:
        return True
    ka, kb = sound_key(a), sound_key(b)
    if not ka or ka != kb:
        return False
    return len(ka) >= 2 or min(len(a), len(b)) >= 3  # "יואב" / "Yoav" -> "b": a one-letter key only for real words


def match_sender(name, participants):  # a name from the model or the user -> the canonical participant, or None
    wanted = _norm(name)
    if not wanted:
        return None
    by_norm = {_norm(p): p for p in participants}
    if wanted in by_norm:
        return by_norm[wanted]
    tokens = _tokens(wanted)
    found = [p for p in participants if tokens and all(any(_same(t, pt) for pt in _tokens(p)) for t in tokens)]
    if len(found) == 1:  # "Omer", "עומר ברנשטיין", "bernstein" -> Omer Bernstein
        return found[0]
    close = difflib.get_close_matches(wanted, list(by_norm), n=2, cutoff=0.8)
    if len(close) == 1 or (close and difflib.SequenceMatcher(None, wanted, close[0]).ratio() == 1):
        return by_norm[close[0]]
    return None


def named_in(question, participant, given=""):  # does the question name this participant (any token, any script)?
    words = {form for word in _tokens(question) if word not in STOP for form in _forms(word)} - STOP
    names = (set(_tokens(participant)) | set(_tokens(given))) - STOP
    return any(_same(w, n) for w in words for n in names if len(n) >= 2)


# ---------------------------------------------------------------------------
# The call and its validation
# ---------------------------------------------------------------------------

def without_names(text, senders):  # drop words that are a sender's name (any script, with Hebrew prefixes)
    names = {t for s in senders for t in _tokens(s)} - STOP
    def is_name(word):
        word = _norm(word).strip("?!.,:;\"'")
        return word not in STOP and any(_same(f, n) for f in _forms(word) - STOP for n in names if len(n) >= 2)
    kept = [w for w in text.split() if not is_name(w)]
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def _valid_bound(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
        else:
            datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def validate(raw, question, participants):
    """The model's JSON -> {query, senders, from, to, dropped: [{field, value, reason}], filtered}."""
    raw = raw if isinstance(raw, dict) else {}
    senders, dropped = [], []
    for name in raw.get("senders") or []:
        name = str(name).strip()
        if not name:
            continue
        canonical = match_sender(name, participants)
        if canonical is None:
            dropped.append({"field": "senders", "value": name, "reason": "not a known participant"})
        elif not named_in(question, canonical, name):
            dropped.append({"field": "senders", "value": name, "reason": "not named in the question"})
        elif canonical not in senders:
            senders.append(canonical)
    bounds = {}
    for field in ("from", "to"):
        value = _valid_bound(raw.get(field))
        if value is None:
            dropped.append({"field": field, "value": raw.get(field), "reason": "not a date"})
            value = ""
        bounds[field] = value
    filtered = bool(senders or bounds["from"] or bounds["to"])
    query = without_names(str(raw.get("query") or ""), senders)  # small models sometimes leave the name in the query
    if filtered and not query:
        query = without_names(question, senders)  # "מה עומר אמר על אמנון?" -> "מה אמר על אמנון?"
    if not filtered or not query:  # nothing to filter: search the question exactly as today
        query = question
    return {"query": query, "senders": senders, "from": bounds["from"], "to": bounds["to"],
            "dropped": dropped, "filtered": filtered}


def extract(question, participants, ask, today=None):
    """ask(prompt, schema) -> dict (one structured-output call). today: date in the chat's time zone."""
    today = today or date.today()
    raw = ask(build_prompt(question, participants, today), SCHEMA)
    return validate(raw, question, participants)
