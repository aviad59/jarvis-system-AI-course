"""Read a supplier's tool result, whatever its shape, into our song records.

  {id, title, status, ready, audio_url, image_url, page_url, duration, style, lyrics}

Songs are found anywhere in the result (a list, "clips", "data": {"songs": [...]}, ...), with snake_case or camelCase
keys. Only https links are kept. If the result is plain text, https audio links in it still count as songs.
"""
import json
import re
from urllib.parse import urlsplit

ID = ("id", "clipid", "songid", "audioid", "trackid")
TITLE = ("title", "songtitle", "name")
STATUS = ("status", "state")
AUDIO = ("audiourl", "songurl", "streamurl", "streamaudiourl", "sourceaudiourl", "sourcestreamaudiourl", "mp3url", "wavurl",
         "downloadurl", "audio")
IMAGE = ("imageurl", "imagelargeurl", "coverurl", "coverimageurl", "sourceimageurl", "thumbnailurl", "thumbnail", "cover", "image")
PAGE = ("pageurl", "shareurl", "weburl", "link", "url")
IDS = ("clipids", "songids", "audioids", "trackids")  # a creation answering only ids: {"clip_ids": ["a", "b"]}
DURATION = ("duration", "durationseconds", "length", "seconds")
STYLE = ("tags", "style", "genre")
LYRICS = ("lyrics", "lyric")
TASK = ("taskid", "jobid", "generationid", "requestid", "batchid")
CREDITS = ("creditsleft", "totalcreditsleft", "remainingcredits", "creditsremaining", "creditbalance", "credits",
           "remaining", "balance")
DONE = {"complete", "completed", "succeeded", "success", "done", "ready", "finished"}
FAILED = {"error", "failed", "failure", "rejected", "cancelled", "canceled", "expired"}
WAITING = {"submitted", "queued", "pending", "processing", "running", "generating", "inprogress", "created", "starting"}
AUDIO_FILE = re.compile(r"\.(?:mp3|wav|m4a|ogg|flac|aac)(?:$|[?#])", re.I)
LINK = re.compile(r"https://[^\s\"'<>()\[\]]+")


def _key(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def payload(structured, texts=(), links=()) -> tuple:
    """(data, text): the result's JSON (structured content, or text that is JSON) and its text."""
    text = "\n".join(t for t in texts if t).strip()
    links = [u for u in links if u]
    data = structured
    if isinstance(data, dict) and set(data) == {"result"}:  # fastmcp wraps lists and plain values like this
        data = data["result"]
    if isinstance(data, str):  # a tool that returns a string: it is the text
        text, data = text or data.strip(), None
    if data is None and text:
        data = _leading_json(text)
    if links:  # resource links in the content (an audio file the supplier points to)
        data = {"result": data, "links": [{"url": u} for u in links]}
    return data, text


def _leading_json(text):
    """The JSON a text is, or starts with: Suno answers '{"clip_ids": [...], ...} This made 2 songs, ... Call audio-wait'."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return json.JSONDecoder().raw_decode(text)[0] if text[:1] in "{[" else None
    except ValueError:
        return None


def https(value) -> str | None:
    value = str(value or "").strip()
    parts = urlsplit(value)
    return value[:2000] if parts.scheme == "https" and parts.hostname and not parts.username else None


def _find(record, keys, nested=True):
    """The first value under one of `keys` (normalized), looking one level into child dicts ("metadata") too."""
    flat = {_key(k): v for k, v in record.items()}
    for key in keys:
        if flat.get(key) not in (None, "", [], {}):
            return flat[key]
    if nested:
        for child in record.values():
            if isinstance(child, dict):
                found = _find(child, keys, nested=False)
                if found is not None:
                    return found
    return None


def _status(value) -> str:
    return _key(value)[:40] if isinstance(value, str) else ""


def ready(song) -> bool:  # playable: done, or audio is there and the supplier doesn't say it's still waiting
    status = song.get("status") or ""
    return status in DONE or (bool(song.get("audio_url")) and status not in WAITING and status not in FAILED)


def failed(song) -> bool:
    return (song.get("status") or "") in FAILED


def _song(record) -> dict | None:
    audio = _find(record, AUDIO)
    url = https(_find(record, ("url",), nested=False))
    if not audio and url and AUDIO_FILE.search(url):  # {"url": ".../song.mp3"}
        audio = url
    audio = https(audio) if isinstance(audio, str) else None
    song_id = _find(record, ID, nested=False)
    song_id = str(song_id)[:100] if isinstance(song_id, (str, int)) and not isinstance(song_id, bool) else None
    title, status = _find(record, TITLE, nested=False), _status(_find(record, STATUS, nested=False))
    image = _find(record, IMAGE)
    if not (audio or (song_id and (title or status or image))):
        return None
    page = _find(record, PAGE, nested=False)
    page = https(page) if isinstance(page, str) and page != audio else None
    duration = _find(record, DURATION)
    style, lyrics = _find(record, STYLE), _find(record, LYRICS)
    song = {"id": song_id, "title": str(title)[:200] if isinstance(title, (str, int)) else "", "status": status,
            "audio_url": audio, "image_url": https(image) if isinstance(image, str) else None, "page_url": page,
            "duration": round(float(duration), 1) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
            "style": (", ".join(map(str, style)) if isinstance(style, list) else str(style or ""))[:300],
            "lyrics": str(lyrics)[:5000] if isinstance(lyrics, str) else ""}
    song["ready"] = ready(song)
    return song


def songs(data, text="") -> list[dict]:
    found, seen = [], set()

    def walk(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, dict):
            song = _song(value)
            if song:
                key = song["id"] or song["audio_url"]
                if key not in seen:
                    seen.add(key)
                    found.append(song)
                return  # a song's own children (metadata, a second audio link) aren't more songs
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:100]:
                walk(child, depth + 1)
    walk(data)
    for song_id in _ids(data):  # ids without a record yet: pending songs, so create_song waits for them
        if song_id not in seen:
            seen.add(song_id)
            found.append({"id": song_id, "title": "", "status": "pending", "audio_url": None, "image_url": None,
                          "page_url": None, "duration": None, "style": "", "lyrics": "", "ready": False})
    if not found and text:  # plain text: its https audio links
        for url in dict.fromkeys(LINK.findall(text)):
            url = url.rstrip(".,;")
            if AUDIO_FILE.search(url):
                found.append({"id": None, "title": "", "status": "", "audio_url": url, "image_url": None, "page_url": None,
                              "duration": None, "style": "", "lyrics": "", "ready": True})
    return found


def _ids(data, depth=0) -> list[str]:
    if not isinstance(data, dict) or depth > 2:
        return []
    value = _find(data, IDS, nested=False)
    if isinstance(value, list):
        return [str(v)[:100] for v in value[:12] if isinstance(v, (str, int)) and not isinstance(v, bool)]
    return next((found for child in data.values() if (found := _ids(child, depth + 1))), [])


def task_id(data) -> str | None:  # a job to poll when the supplier answers "started" without songs
    if isinstance(data, dict):
        value = _find(data, TASK)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return str(value)[:100]
        for child in data.values():
            found = task_id(child)
            if found:
                return found
    elif isinstance(data, list):
        for child in data[:20]:
            found = task_id(child)
            if found:
                return found
    return None


def credits(data, text="") -> int | float | None:
    def walk(value, depth=0):
        if depth > 6:
            return None
        if isinstance(value, dict):
            flat = {_key(k): v for k, v in value.items()}
            for key in CREDITS:
                found = flat.get(key)
                if isinstance(found, (int, float)) and not isinstance(found, bool):
                    return found
            for child in value.values():
                found = walk(child, depth + 1)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value[:20]:
                found = walk(child, depth + 1)
                if found is not None:
                    return found
        return None
    found = walk(data)
    if found is None and text:
        match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*credits?\b", text, re.I) or re.search(r"credits?\D{0,20}?(\d[\d,]*)", text, re.I)
        if match:
            number = float(match.group(1).replace(",", ""))
            found = int(number) if number.is_integer() else number
    return found


def brief(data, text="", limit=1500) -> str:  # the raw answer, short, for details the tools don't normalize
    raw = json.dumps(data, ensure_ascii=False, default=str) if data is not None else text
    return raw[:limit] + ("…" if len(raw) > limit else "")
