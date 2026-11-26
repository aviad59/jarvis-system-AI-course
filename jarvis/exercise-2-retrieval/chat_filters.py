"""Sender and time filters for chat chunks, as one MongoDB filter that runs inside $vectorSearch.

  sender filter: the chunk's participants contain any of the senders
  time filter:   the chunk overlaps the range: start_ts <= to AND end_ts >= from (either bound optional)

Bounds are local times in CHAT_TIMEZONE (default Asia/Jerusalem) unless they carry an offset; a date
alone covers the whole day (from = 00:00, to = 23:59:59.999). Stored times are UTC.
"""
import os  # CHAT_TIMEZONE
import re  # date-only bounds
from datetime import date, datetime, time, timezone

DEFAULT_TIMEZONE = "Asia/Jerusalem"


def chat_timezone(name=None):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = name or os.environ.get("CHAT_TIMEZONE") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"Unknown time zone {name!r}. On Windows run: python -m pip install tzdata") from None


def parse_bound(value, end=False, tz=None):  # "2026-08-05" / "2026-08-05T14:30" / datetime -> aware UTC datetime, or None
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    tz = tz or chat_timezone()
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time(23, 59, 59, 999000) if end else time.min)
    else:
        text = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            moment = datetime.combine(date.fromisoformat(text), time(23, 59, 59, 999000) if end else time.min)
        else:
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"Not a date or date-time: {value!r} (use YYYY-MM-DD or YYYY-MM-DDTHH:MM)") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(timezone.utc)


def normalize(senders=None, date_from=None, date_to=None, tz=None):
    """-> {'senders': [...], 'from': UTC datetime or None, 'to': UTC datetime or None}; a reversed range is swapped."""
    if isinstance(senders, str):
        senders = senders.split(",")
    names = list(dict.fromkeys(s.strip() for s in (senders or []) if s and s.strip()))
    start, end = parse_bound(date_from, False, tz), parse_bound(date_to, True, tz)
    if start and end and start > end:
        start, end = parse_bound(date_to, False, tz), parse_bound(date_from, True, tz)
    return {"senders": names, "from": start, "to": end}


def active(filters):  # any filter at all?
    return bool(filters and (filters["senders"] or filters["from"] or filters["to"]))


def mongo_filter(filters):  # the $vectorSearch / find() filter, or None for no filter
    clauses = []
    if filters["senders"]:
        clauses.append({"participants": {"$in": filters["senders"]}})
    if filters["to"]:
        clauses.append({"start_ts": {"$lte": filters["to"]}})
    if filters["from"]:
        clauses.append({"end_ts": {"$gte": filters["from"]}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def combine(*parts):  # AND of several filters, skipping None
    parts = [p for p in parts if p]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"$and": parts}


def relaxations(filters):
    """The attempts to make, in order, as (relaxed, filters): exact first, then without the time range,
    then without the senders, then without any filter. 'relaxed' names what was dropped."""
    attempts = [([], filters)]
    has_time, has_senders = bool(filters["from"] or filters["to"]), bool(filters["senders"])
    if has_time and has_senders:
        attempts.append((["time"], {**filters, "from": None, "to": None}))
        attempts.append((["senders"], {**filters, "senders": []}))
    if has_time or has_senders:
        attempts.append(([n for n, on in (("senders", has_senders), ("time", has_time)) if on],
                         {"senders": [], "from": None, "to": None}))
    return attempts


def _utc(value):  # stored dates come back naive (UTC) from pymongo, ISO strings from build/index.json
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def matches(chunk, filters):  # the same rule as mongo_filter, for a chunk dict (tests, local checks)
    if filters["senders"] and not set(filters["senders"]) & set(chunk.get("participants") or []):
        return False
    if (filters["from"] or filters["to"]) and not (chunk.get("start_ts") and chunk.get("end_ts")):
        return False  # notes have no times: a time filter never matches them
    if filters["to"] and _utc(chunk["start_ts"]) > filters["to"]:
        return False
    if filters["from"] and _utc(chunk["end_ts"]) < filters["from"]:
        return False
    return True


def describe(filters):  # JSON-friendly copy for the results file
    stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None  # noqa: E731
    return {"senders": filters["senders"], "from": stamp(filters["from"]), "to": stamp(filters["to"])}
