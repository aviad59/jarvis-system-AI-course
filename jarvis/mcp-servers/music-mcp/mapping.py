"""Our fixed tools -> the supplier's tools. Explicit mappings in suppliers.json win; otherwise we guess from the
supplier's own tool names and descriptions, so a new supplier often works without any mapping.

  "ops": {"create_song": {"tool": "generate_music", "args": {"description": "prompt", "style": "tags"},
                          "fixed": {"model": "v5"}},
          "get_song": "get_music_status"}           # just the tool name: the arguments are matched by name

An "args" entry mapped to null is never sent.
"""
import re

OPS = ("create_song", "get_credits", "list_songs", "get_song")  # the order matters: credits before get, so "get_credits"
#                                                                  isn't taken for get_song
VERBS = {"create_song": ("generate", "create", "make", "compose", "new"),
         "get_song": ("status", "get", "fetch", "wait", "check", "retrieve", "poll", "info", "details"),
         "list_songs": ("list", "library", "recent", "history", "search", "my"),
         "get_credits": ("credit", "usage", "balance", "quota", "billing", "account")}
NOUNS = ("song", "music", "track", "tune", "clip", "audio", "generation", "task")
NOT_FOR = {"create_song": ("status", "list", "lyric", "stem", "video", "cover", "extend", "remix", "upload", "sfx", "image"),
           "get_song": ("list", "credit", "lyric", "generate", "create"),
           "list_songs": ("credit", "generate", "create")}
ARGS = {"description": ("description", "prompt", "gpt_description_prompt", "song_description", "idea", "topic", "text", "query"),
        "lyrics": ("lyrics", "lyric", "custom_lyrics"),
        "style": ("style", "styles", "tags", "genre", "music_style"),
        "title": ("title", "song_title", "name"),
        "instrumental": ("instrumental", "make_instrumental", "is_instrumental"),
        "song_id": ("song_id", "clip_id", "id", "task_id", "ids", "clip_ids", "song_ids", "job_id"),
        "limit": ("limit", "count", "page_size", "per_page", "max_results", "n")}


def words(text) -> list[str]:  # "getMusicStatus" / "get_music-status" -> ["get", "music", "status"]
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


ENDINGS = ("", "s", "es", "ed", "ing", "d")


def _hits(found, wanted) -> list[int]:  # indexes in `wanted` that a word is ("credits" hits "credit", "listen" not "list")
    return [i for i, w in enumerate(wanted) if any(f in {w + e for e in ENDINGS} for f in found)]


def score(op, tool) -> float:
    """How well a supplier tool fits one of our ops; 0 = not at all. The verb must be in the tool's name."""
    name, about = words(tool["name"]), words(tool.get("description", ""))
    verbs = _hits(name, VERBS[op])
    if not verbs or _hits(name, NOT_FOR.get(op, ())):
        return 0
    value = 3 + 0.1 * (len(VERBS[op]) - min(verbs))  # an earlier verb in the list is the better match
    if op == "get_credits":
        return value
    nouns = bool(_hits(name, NOUNS)) * 2 + bool(_hits(about, NOUNS))
    if op == "create_song" and not nouns:
        return 0
    return value + nouns + bool(_hits(about, VERBS[op])) * 0.5


def auto_map(tools) -> dict:  # {op: tool name or None}; each supplier tool is used for one op at most
    chosen, used = {}, set()
    for op in OPS:
        ranked = sorted(((score(op, t), -i, t["name"]) for i, t in enumerate(tools) if t["name"] not in used), reverse=True)
        best = ranked[0] if ranked and ranked[0][0] > 0 else None
        chosen[op] = best[2] if best else None
        if best:
            used.add(best[2])
    return chosen


def plan(op, tools, ops) -> dict:
    """The supplier tool for one op: {"tool": {...}, "args": {...} or None, "fixed": {...}}. LookupError if none."""
    by_name = {t["name"]: t for t in tools}
    given = ops.get(op)
    if isinstance(given, str):
        given = {"tool": given}
    if given:
        if not isinstance(given, dict) or given.get("tool") not in by_name:
            raise LookupError(f"suppliers.json maps {op} to {given.get('tool') if isinstance(given, dict) else given!r}, "
                              f"but the supplier offers: {', '.join(by_name) or 'no tools'}")
        return {"tool": by_name[given["tool"]], "args": given.get("args"), "fixed": given.get("fixed") or {},
                "modes": bool(given.get("description_or_lyrics"))}
    name = auto_map(tools)[op]
    if not name:
        raise LookupError(f"The supplier has no tool for {op} (it offers: {', '.join(by_name) or 'no tools'}); "
                          f"map it in suppliers.json under ops.{op}")
    return {"tool": by_name[name], "args": None, "fixed": {}, "modes": False}


def _key(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _cast(value, spec):
    kind = spec.get("type") if isinstance(spec, dict) else None
    if kind == "array" and not isinstance(value, list):
        return [value]
    if kind == "integer" and not isinstance(value, bool):
        return int(value)
    if kind == "boolean":
        return bool(value)
    if kind == "string" and not isinstance(value, str):
        return str(value).lower() if isinstance(value, bool) else str(value)
    return value


def build_args(ours, schema, explicit=None, fixed=None, modes=False):
    """Our arguments -> the supplier tool's. Returns (arguments, dropped: our keys the tool has no place for).

    Matching is by name (ARGS synonyms, case and _ ignored). What doesn't fit and is about the song (style, title,
    instrumental, lyrics) is written into the description instead, when the tool has one.
    modes=True ("description_or_lyrics" in suppliers.json, Suno): the tool takes a description alone OR lyrics with a
    style and title, never both. With lyrics the description isn't sent; without, style and title go into it."""
    properties = (schema or {}).get("properties") or {}
    if modes and ours.get("lyrics"):
        ours = {k: v for k, v in ours.items() if k != "description"}
    elif modes:
        explicit = {**(explicit or {}), "style": "", "title": ""}  # "" = no field: folded into the description
    by_key = {_key(p): p for p in properties}
    args, dropped, filled, description = {}, [], set(), None
    for key, value in ours.items():
        if value in (None, "") or (value is False and key == "instrumental"):
            continue
        if explicit and key in explicit:
            target = explicit[key] or None
            if explicit[key] is None:
                continue
        elif not properties:  # the tool doesn't describe its arguments: send ours as they are
            target = key
        else:
            target = next((by_key[_key(s)] for s in ARGS.get(key, (key,)) if _key(s) in by_key and by_key[_key(s)] not in filled), None)
        if target is None:
            dropped.append(key)
            continue
        args[target] = _cast(value, properties.get(target))
        filled.add(target)
        if key == "description":
            description = target
    if "description" in dropped:  # no field matched by name: the tool's one required text field is the description
        required = [p for p in (schema or {}).get("required") or [] if p not in filled
                    and (properties.get(p) or {}).get("type") in ("string", None)]
        if len(required) == 1:
            description = required[0]
            args[description] = ours["description"]
            dropped.remove("description")
    folded = [k for k in ("style", "title", "instrumental", "lyrics") if k in dropped]
    if folded and description and isinstance(args.get(description), str):  # the description carries what has no field
        extra = {"style": f"Style: {ours.get('style')}.", "title": f"Title: {ours.get('title')}.",
                 "instrumental": "Instrumental, no vocals.", "lyrics": f"Lyrics:\n{ours.get('lyrics')}"}
        args[description] = " ".join([args[description].rstrip(), *(extra[k] for k in folded)])
        dropped = [k for k in dropped if k not in folded]
    args.update(fixed or {})
    return args, dropped
