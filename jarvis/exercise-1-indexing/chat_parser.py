"""WhatsApp chat exports (Markdown) -> messages -> message-aligned chunks.

The export format (as written into the Obsidian vault):

    # WhatsApp Chat Export: <chat name>
    Export date: ...
    ---
    ## 30 ביולי 2026                       <- day header: the date of the messages below
    [11:19] **Idan Aviad:** first line     <- [H:MM] **Sender:** text
    more lines of the same message         <- a message runs until the next message or day header
    [11:57] **Omer Bernstein:**            <- a reply: the quoted message comes first
    > _Yoav Elkayam: 2.5%_
    the reply text

Each chunk keeps three views of the same messages:
  embed_text    - message contents only (no names, no times): what the embedding model sees
  display_text  - "Sender [YYYY-MM-DD HH:MM]: text" per message: what the answer model sees
  messages      - [{sender, ts, text, reply_to?}]: structured attribution
Chunk boundaries always fall between messages. Times are local to CHAT_TIMEZONE (default
Asia/Jerusalem) and stored in UTC. On Windows, zoneinfo needs: python -m pip install tzdata
"""
import os  # CHAT_TIMEZONE
import re  # line formats
import unicodedata  # name normalization
from datetime import datetime, timezone  # message times

DEFAULT_TIMEZONE = "Asia/Jerusalem"
EXPORT_TITLE = re.compile(r"^#\s+WhatsApp Chat Export\b[:\s-]*(.*)$")  # the first line of an export
DAY_HEADER = re.compile(r"^##\s+(.+?)\s*$")
MESSAGE = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp]\.?[Mm]\.?)?\]\s+\*\*(.+?):\*\*[ \t]?(.*)$")
QUOTE = re.compile(r"^>\s*_(.+?):\s(.*)_\s*$")  # > _Sender: quoted text_
SEPARATOR = re.compile(r"^-{3,}\s*$")
INVISIBLE = re.compile("[​-‏‪-‮⁦-⁩﻿]")  # bidi marks WhatsApp puts around names
MEDIA = re.compile(r"\[(?:Sticker|Image|Video|Audio|Voice message|GIF|Document|album message|poll_creation|"
                   r"Forwarded|Deleted message|View once)\]", re.I)  # placeholders, not content
VCARD = re.compile(r"\[Contact:\s*BEGIN:VCARD.*?END:VCARD\]", re.S)
VCARD_NAME = re.compile(r"^FN:(.*)$", re.M)

HEBREW_MONTHS = {"ינואר": 1, "פברואר": 2, "מרץ": 3, "מרס": 3, "אפריל": 4, "מאי": 5, "יוני": 6, "יולי": 7,
                 "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12}
ENGLISH_MONTHS = {name: number for number, names in enumerate(
    (("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
     ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
     ("november", "nov"), ("december", "dec")), 1) for name in names}


def chat_timezone(name=None):  # the source timezone of the export's local times
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = name or os.environ.get("CHAT_TIMEZONE") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"Unknown time zone {name!r}. On Windows run: python -m pip install tzdata") from None


def is_chat_export(text):  # True for a WhatsApp export (its first non-empty line is the export title)
    first = next((line for line in text.lstrip("﻿").splitlines() if line.strip()), "")
    return bool(EXPORT_TITLE.match(first.strip()))


def canonical_sender(name):  # one spelling per person: NFC, no bidi marks / "~" prefix, single spaces, trimmed
    name = INVISIBLE.sub("", unicodedata.normalize("NFC", name))
    name = re.sub(r"\s+", " ", name).strip()
    return name.lstrip("~").strip()


def parse_day(text):  # "30 ביולי 2026" / "30 July 2026" / "July 30, 2026" / "2026-07-30" / "30/07/2026" -> (y, m, d)
    text = text.strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        return _date(int(match[1]), int(match[2]), int(match[3]))
    match = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)  # day first, as in Israeli exports
    if match:
        return _date(int(match[3]), int(match[2]), int(match[1]))
    match = re.fullmatch(r"(\d{1,2})\s+ב?-?(\S+?),?\s+(\d{4})", text)  # 30 ביולי 2026 / 30 July 2026
    if match:
        month = _month(match[2])
        if month:
            return _date(int(match[3]), month, int(match[1]))
    match = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text)  # July 30, 2026
    if match and _month(match[1]):
        return _date(int(match[3]), _month(match[1]), int(match[2]))
    return None


def _month(word):
    word = word.strip(".").lower()
    if word in HEBREW_MONTHS:
        return HEBREW_MONTHS[word]
    if word[:1] == "ב" and word[1:] in HEBREW_MONTHS:  # "ביולי"
        return HEBREW_MONTHS[word[1:]]
    return ENGLISH_MONTHS.get(word)


def _date(year, month, day):
    try:
        datetime(year, month, day)
    except ValueError:
        return None
    return year, month, day


def _clean_text(lines):  # body lines -> text without the trailing day-separator and blank lines
    while lines and (not lines[-1].strip() or SEPARATOR.match(lines[-1])):
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(line.rstrip() for line in lines)


def parse_chat(text, tz=None):
    """Return {'title', 'messages': [...]} for one export.

    Each message: {'sender', 'ts' (aware UTC datetime), 'local' (naive local datetime), 'text',
    'reply_to': {'sender', 'text'} or None, 'start', 'end' (character offsets of the message in text)}.
    Raises ValueError for a message before any day header (its date is unknown).
    """
    tz = tz or chat_timezone()
    text = text.lstrip("﻿").replace("\r\n", "\n")
    title, day, messages, current = "", None, [], None
    position = 0

    def close(end):  # finish the message being read; end = offset of the line that follows it
        if current is None:
            return
        first, body, reply = current.pop("_first"), current.pop("_lines"), None
        if not first and body and QUOTE.match(body[0]):  # "[t] **X:**" then "> _Y: ..._": a reply
            quote = QUOTE.match(body.pop(0))
            reply = {"sender": canonical_sender(quote[1]), "text": quote[2].strip()}
        current["text"] = _clean_text(([first] if first else []) + body)
        current["reply_to"] = reply
        raw = text[current["start"]:end].split("\n")
        while raw and (not raw[-1].strip() or SEPARATOR.match(raw[-1])):  # the blank lines / --- after it
            raw.pop()
        current["end"] = current["start"] + len("\n".join(raw))
        messages.append(current)

    for number, line in enumerate(text.split("\n"), 1):
        line_start, position = position, position + len(line) + 1
        stripped = line.strip()
        if not title and not messages and current is None and EXPORT_TITLE.match(stripped):
            title = EXPORT_TITLE.match(stripped)[1].strip()
            continue
        header = DAY_HEADER.match(stripped)
        if header and parse_day(header[1]):
            close(line_start)
            current, day = None, parse_day(header[1])
            continue
        match = MESSAGE.match(line)
        if match:
            close(line_start)
            if day is None:
                raise ValueError(f"Line {number}: message before any date header")
            hour, minute, second = int(match[1]), int(match[2]), int(match[3] or 0)
            if match[4]:  # 12-hour clock
                pm = match[4].lower().startswith("p")
                hour = hour % 12 + (12 if pm else 0)
            local = datetime(*day, hour, minute, second)
            utc = local.replace(tzinfo=tz).astimezone(timezone.utc)
            current = {"sender": canonical_sender(match[5]), "ts": utc, "local": local,
                       "_first": match[6].rstrip(), "_lines": [], "start": line_start}
            continue
        if current is not None:
            current["_lines"].append(line)
    close(len(text))
    return {"title": title, "messages": messages}


# ---------------------------------------------------------------------------
# The three views of a message
# ---------------------------------------------------------------------------

def display_line(message):  # "Sender [YYYY-MM-DD HH:MM]: text" (who said it, when, and what)
    stamp = message["local"].strftime("%Y-%m-%d %H:%M")
    reply = message.get("reply_to")
    prefix = f"(replying to {reply['sender']}: \"{reply['text']}\") " if reply else ""
    return f"{message['sender']} [{stamp}]: {prefix}{message['text']}".rstrip()


def content_only(text):  # message contents without media placeholders; a contact card becomes its name
    text = VCARD.sub(lambda m: "Contact: " + ((VCARD_NAME.search(m[0]) or [None, ""])[1].strip()), text)
    text = MEDIA.sub(" ", text)
    text = re.sub(r"\*\*(Poll:.*?)\*\*", r"\1", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n", "\n", text)).strip()


def embed_line(message):  # what the embedding model sees: no sender, no time; a reply keeps the quoted words
    own = content_only(message["text"])
    reply = message.get("reply_to")
    quoted = content_only(reply["text"]) if reply else ""
    if quoted and own:
        return f"> {quoted}\n{own}"
    return own  # a media-only reply adds nothing


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------

def chunk_messages(messages, max_chars=900):
    """Group consecutive messages into chunks, the existing rule with whole messages as the units.

    Messages are merged while the chunk's display_text stays within max_chars; a single message
    longer than that becomes a chunk of its own. A message is never split.
    """
    groups, current, size = [], [], 0
    for message in messages:
        length = len(display_line(message))
        if current and size + 1 + length <= max_chars:
            current.append(message)
            size += 1 + length
            continue
        if current:
            groups.append(current)
        current, size = [message], length
    if current:
        groups.append(current)
    return groups


def build_chunks(chat_file, text, tz=None, max_chars=900):
    """chat_file (a stable name, e.g. the path in the vault) + export text -> (title, [chunk dicts]).

    Chunk: {chat_file, chunk_index, embed_text, display_text, messages, participants, start_ts, end_ts,
    start, end}. Times are aware UTC datetimes; messages carry {sender, ts, text, reply_to?}.
    """
    parsed = parse_chat(text, tz)
    if not parsed["messages"]:
        raise ValueError(f"No messages found in {chat_file}")
    chunks = []
    for index, group in enumerate(chunk_messages(parsed["messages"], max_chars)):
        participants = list(dict.fromkeys(m["sender"] for m in group))  # distinct, in order of appearance
        embed_text = "\n".join(line for line in (embed_line(m) for m in group) if line)
        chunks.append({
            "chat_file": chat_file, "chunk_index": index,
            "embed_text": embed_text or "[media]",  # a chunk of stickers only still needs some input
            "display_text": "\n".join(display_line(m) for m in group),
            "messages": [{"sender": m["sender"], "ts": m["ts"], "text": m["text"],
                          **({"reply_to": m["reply_to"]} if m["reply_to"] else {})} for m in group],
            "participants": participants,
            "start_ts": group[0]["ts"], "end_ts": group[-1]["ts"],
            "start": group[0]["start"], "end": group[-1]["end"],
        })
    return parsed["title"], chunks


def iso(ts):  # UTC datetime -> "2026-07-30T08:19:00Z" (JSON files); Mongo stores the datetime itself
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text):  # inverse of iso(): aware UTC datetime
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def chunk_id(chat_file, chunk_index):  # also the Mongo _id; unique per (chat_file, chunk_index)
    return f"chat:{chat_file}:{chunk_index}"


def index_records(document, max_chars=900, tz=None):
    """A chat document from the vault loader -> records for build/index.json (same contract as note chunks).

    text is the display_text (what the answer model reads) and embedding_text the embed_text, so
    build_index.py embed computes the vector from the contents only. The structured fields are kept
    under 'chat' (times as ISO strings) for the MongoDB sync.
    """
    title, chunks = build_chunks(document["path"], document["body"], tz, max_chars)
    records = []
    for chunk in chunks:
        metadata = {field: document[field] for field in ("document_id", "version", "status", "updated", "source", "title")}
        metadata.update(path=document["path"], kind="chat", chat_file=chunk["chat_file"],
                        participants=chunk["participants"], start_ts=iso(chunk["start_ts"]), end_ts=iso(chunk["end_ts"]))
        records.append({
            "chunk_id": chunk_id(chunk["chat_file"], chunk["chunk_index"]), "kind": "chat",
            "text": chunk["display_text"], "embedding_text": chunk["embed_text"],
            "start": chunk["start"], "end": chunk["end"], "metadata": metadata,
            "document_sha256": document["document_sha256"],
            "chat": {"chat_file": chunk["chat_file"], "chunk_index": chunk["chunk_index"],
                     "participants": chunk["participants"], "start_ts": iso(chunk["start_ts"]),
                     "end_ts": iso(chunk["end_ts"]),
                     "messages": [{**m, "ts": iso(m["ts"])} for m in chunk["messages"]]},
        })
    return records


def embedding_input(title, embed_text, document_format):  # exactly what build_index.py embed sends to the model
    return f"title: {title} | text: {embed_text}" if document_format == "embeddinggemma" else embed_text
