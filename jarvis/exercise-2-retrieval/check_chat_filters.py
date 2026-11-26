"""Tests for the chat filters, the filtered $vectorSearch and the self-query step (no network, no database).

  python check_chat_filters.py
"""
import argparse
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import chat_filters as cf
import search
import self_query as sq

TZ = cf.chat_timezone("Asia/Jerusalem")
UTC = timezone.utc
PEOPLE = ["Yarden Nazarestsky", "Omer Bernstein", "מחליף של בר ענבר", "Yoav Elkayam", "You", "Bar Ventura", "Idan Aviad"]


def chunk(participants, start, end):
    return {"participants": participants, "start_ts": start, "end_ts": end}


AUG3 = chunk(["Omer Bernstein", "Yarden Nazarestsky"], datetime(2026, 8, 3, 9, 0), datetime(2026, 8, 3, 18, 0))
AUG4_5 = chunk(["Yoav Elkayam"], datetime(2026, 8, 4, 20, 0), datetime(2026, 8, 5, 8, 0))
NOTE = {"text": "a vault note"}  # no participants, no times


class Bounds(unittest.TestCase):
    def test_date_only_covers_the_whole_local_day(self):
        self.assertEqual(cf.parse_bound("2026-08-05", tz=TZ), datetime(2026, 8, 4, 21, 0, tzinfo=UTC))
        self.assertEqual(cf.parse_bound("2026-08-05", end=True, tz=TZ), datetime(2026, 8, 5, 20, 59, 59, 999000, tzinfo=UTC))

    def test_time_and_offset(self):
        self.assertEqual(cf.parse_bound("2026-11-12T09:30", tz=TZ), datetime(2026, 11, 12, 7, 30, tzinfo=UTC))
        self.assertEqual(cf.parse_bound("2026-11-12T09:30Z", tz=TZ), datetime(2026, 11, 12, 9, 30, tzinfo=UTC))

    def test_empty_and_invalid(self):
        self.assertIsNone(cf.parse_bound("", tz=TZ))
        self.assertIsNone(cf.parse_bound(None, tz=TZ))
        with self.assertRaises(ValueError):
            cf.parse_bound("last week", tz=TZ)

    def test_reversed_range_is_swapped(self):
        f = cf.normalize(None, "2026-08-05", "2026-08-03", TZ)
        self.assertLess(f["from"], f["to"])
        self.assertEqual(f["from"], cf.parse_bound("2026-08-03", tz=TZ))


class FilterLogic(unittest.TestCase):
    def test_none(self):
        f = cf.normalize(tz=TZ)
        self.assertFalse(cf.active(f))
        self.assertIsNone(cf.mongo_filter(f))
        self.assertEqual(cf.relaxations(f), [([], f)])
        self.assertTrue(cf.matches(NOTE, f) and cf.matches(AUG3, f))

    def test_sender_only(self):
        f = cf.normalize("Omer Bernstein, Bar Ventura", tz=TZ)
        self.assertEqual(cf.mongo_filter(f), {"participants": {"$in": ["Omer Bernstein", "Bar Ventura"]}})
        self.assertTrue(cf.matches(AUG3, f))  # any of the senders
        self.assertFalse(cf.matches(AUG4_5, f))
        self.assertFalse(cf.matches(NOTE, f))

    def test_time_only_overlap(self):
        f = cf.normalize(None, "2026-08-05", "2026-08-05", TZ)
        self.assertEqual(cf.mongo_filter(f), {"$and": [{"start_ts": {"$lte": f["to"]}}, {"end_ts": {"$gte": f["from"]}}]})
        self.assertTrue(cf.matches(AUG4_5, f))  # starts on the 4th, overlaps the 5th
        self.assertFalse(cf.matches(AUG3, f))
        self.assertFalse(cf.matches(NOTE, f))
        open_end = cf.normalize(None, "2026-08-04", None, TZ)  # from only
        self.assertEqual(cf.mongo_filter(open_end), {"end_ts": {"$gte": open_end["from"]}})
        self.assertTrue(cf.matches(AUG4_5, open_end) and not cf.matches(AUG3, open_end))

    def test_both(self):
        f = cf.normalize(["Yoav Elkayam"], "2026-08-01", "2026-08-31", TZ)
        self.assertEqual(set(cf.mongo_filter(f)), {"$and"})
        self.assertEqual(len(cf.mongo_filter(f)["$and"]), 3)
        self.assertTrue(cf.matches(AUG4_5, f))
        self.assertFalse(cf.matches(AUG3, f))  # right month, wrong sender

    def test_relaxation_order(self):
        f = cf.normalize(["Yoav Elkayam"], "2026-09-01", "2026-09-30", TZ)
        self.assertEqual([relaxed for relaxed, _ in cf.relaxations(f)], [[], ["time"], ["senders"], ["senders", "time"]])
        self.assertEqual([r for r, _ in cf.relaxations(cf.normalize(["A"], tz=TZ))], [[], ["senders"]])

    def test_combine(self):
        self.assertIsNone(cf.combine(None, None))
        self.assertEqual(cf.combine({"a": 1}, None), {"a": 1})
        self.assertEqual(cf.combine({"a": 1}, {"b": 2}), {"$and": [{"a": 1}, {"b": 2}]})


class FakeCollection:  # records every $vectorSearch stage; returns the next canned result list
    def __init__(self, answers):
        self.answers, self.stages = list(answers), []

    def aggregate(self, pipeline):
        self.stages.append(pipeline[0]["$vectorSearch"])
        return self.answers.pop(0)

    def estimated_document_count(self):
        return 43


class FakeDb(dict):
    def __init__(self, collection):
        super().__init__(chunks=collection)


def hit(chunk_id="chat:chat.md:3"):
    return {"chunk_id": chunk_id, "text": "Omer Bernstein [2026-08-03 12:00]: hi", "kind": "chat",
            "metadata": {"document_id": "chat", "version": "1", "status": "current", "source": "vault/chat.md"},
            "messages": [{"sender": "Omer Bernstein", "ts": datetime(2026, 8, 3, 9, 0), "text": "hi"}],
            "participants": ["Omer Bernstein"], "start_ts": datetime(2026, 8, 3, 9, 0), "end_ts": datetime(2026, 8, 3, 9, 0),
            "score": 0.9}


MANIFEST = {"embedding_mode": "ollama", "model": "m", "model_digest": "d", "dimensions": 3, "document_format": "none"}
SETTINGS = {"collection": "chunks", "vector_index": "vector_index"}
ARGS = argparse.Namespace(top_k=3, exact=False, exclude_superseded=False)


@patch.object(search, "load_manifest", lambda db, collection: MANIFEST)
@patch.object(search, "query_embedding", lambda question, manifest, args: [0.1, 0.2, 0.3])
class FilteredSearch(unittest.TestCase):
    def test_filters_run_inside_vector_search(self):
        collection = FakeCollection([[hit()]])
        f = cf.normalize(["Omer Bernstein"], "2026-08-03", "2026-08-03", TZ)
        _, results, _, info = search.filtered_search(ARGS, "q", FakeDb(collection), SETTINGS, f)
        stage = collection.stages[0]
        self.assertEqual(stage["filter"], cf.mongo_filter(f))  # in the same query, before the top-k
        self.assertTrue(stage["exact"])
        self.assertEqual(stage["limit"], 3)
        self.assertEqual(info["relaxed"], [])
        self.assertEqual(results[0]["messages"][0]["ts"], "2026-08-03T09:00:00Z")  # JSON-ready
        self.assertEqual(results[0]["participants"], ["Omer Bernstein"])
        self.assertAlmostEqual(results[0]["score"], 0.8)

    def test_zero_results_relaxes_and_says_so(self):
        collection = FakeCollection([[], [hit()]])
        f = cf.normalize(["Omer Bernstein"], "2026-09-01", "2026-09-02", TZ)
        _, results, _, info = search.filtered_search(ARGS, "q", FakeDb(collection), SETTINGS, f)
        self.assertEqual(info["relaxed"], ["time"])
        self.assertEqual(collection.stages[1]["filter"], {"participants": {"$in": ["Omer Bernstein"]}})
        self.assertEqual(len(results), 1)

    def test_no_filter_keeps_the_unfiltered_stage(self):
        collection = FakeCollection([[hit("note-1")]])
        search.atlas_vector_search(ARGS, "q", FakeDb(collection), SETTINGS)
        self.assertNotIn("filter", collection.stages[0])
        self.assertIn("numCandidates", collection.stages[0])

    def test_superseded_filter_is_combined(self):
        collection = FakeCollection([[hit()]])
        args = argparse.Namespace(top_k=3, exact=False, exclude_superseded=True)
        search.atlas_vector_search(args, "q", FakeDb(collection), SETTINGS, {"participants": {"$in": ["A"]}}, exact=True)
        self.assertEqual(collection.stages[0]["filter"],
                         {"$and": [{"participants": {"$in": ["A"]}}, {"metadata.status": {"$ne": "superseded"}}]})


class SourceFile(unittest.TestCase):
    def test_filters_need_mongodb(self):
        args = argparse.Namespace(question="q", query=None, senders="Omer Bernstein", date_from=None, date_to=None,
                                  timezone="Asia/Jerusalem", source="file")
        with self.assertRaisesRegex(ValueError, "need MongoDB"):
            search.run_search(args)

    def test_note_records_get_no_chat_fields(self):
        self.assertEqual(search.chat_fields({"chunk_id": "x", "text": "t", "metadata": {}}), {})
        record = {"kind": "chat", "chat": {"messages": [{"sender": "A", "ts": "2026-08-03T09:00:00Z", "text": "x"}],
                                           "participants": ["A"], "start_ts": "2026-08-03T09:00:00Z",
                                           "end_ts": "2026-08-03T09:00:00Z"}}
        self.assertEqual(search.chat_fields(record)["participants"], ["A"])


class Names(unittest.TestCase):
    def test_match_sender(self):
        cases = {"Omer Bernstein": "Omer Bernstein", "omer bernstein": "Omer Bernstein", "Omer": "Omer Bernstein",
                 "עומר": "Omer Bernstein", "עומר ברנשטיין": "Omer Bernstein", "ירדן": "Yarden Nazarestsky",
                 "Yarden Nazaretsky": "Yarden Nazarestsky", "יואב": "Yoav Elkayam", "מחליף של בר ענבר": "מחליף של בר ענבר",
                 "Bar  Ventura ": "Bar Ventura", "Moshe": None, "": None}
        for given, expected in cases.items():
            self.assertEqual(sq.match_sender(given, PEOPLE), expected, given)

    def test_named_in_question(self):
        self.assertTrue(sq.named_in("מה עומר אמר על הטיול?", "Omer Bernstein"))
        self.assertTrue(sq.named_in("מה אמר ועומר", "Omer Bernstein"))  # prefix letter
        self.assertFalse(sq.named_in("מה ירדן אמר על הטיול?", "Omer Bernstein"))  # "אמר" (said) is not Omer
        self.assertFalse(sq.named_in("מה של הטיול", "מחליף של בר ענבר"))


class SelfQuery(unittest.TestCase):
    TODAY = date(2026, 11, 26)  # a Thursday

    def test_calendar(self):
        text, ranges = sq.calendar(self.TODAY)
        self.assertIn("today is Thursday 2026-11-26", text)
        self.assertEqual(ranges["yesterday"], (date(2026, 11, 25), date(2026, 11, 25)))
        self.assertEqual(ranges["last week"], (date(2026, 11, 15), date(2026, 11, 21)))  # Sunday..Saturday
        self.assertEqual(ranges["last month"], (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(sq.latest(8, 5, self.TODAY), date(2026, 8, 5))
        self.assertEqual(sq.latest(12, 30, self.TODAY), date(2025, 12, 30))

    def test_prompt_has_participants_and_date(self):
        prompt = sq.build_prompt("מה עומר אמר על הטיול בשבוע שעבר?", PEOPLE, self.TODAY)
        self.assertIn("Omer Bernstein", prompt)
        self.assertIn("2026-11-26", prompt)
        self.assertIn("last week / בשבוע שעבר: from 2026-11-15 to 2026-11-21", prompt)
        self.assertIn("מה דובר", prompt)  # the generic query in the question's language

    def extract(self, question, reply):
        seen = {}

        def ask(prompt, schema):
            seen.update(prompt=prompt, schema=schema)
            return reply
        result = sq.extract(question, PEOPLE, ask, self.TODAY)
        self.assertEqual(seen["schema"], sq.SCHEMA)
        return result

    def test_sender_topic_and_last_week(self):
        r = self.extract("מה עומר אמר על הטיול בשבוע שעבר?",
                         {"query": "הטיול", "senders": ["עומר"], "from": "2026-11-15", "to": "2026-11-21"})
        self.assertEqual((r["query"], r["senders"], r["from"], r["to"]), ("הטיול", ["Omer Bernstein"], "2026-11-15", "2026-11-21"))
        self.assertTrue(r["filtered"])
        self.assertEqual(r["dropped"], [])

    def test_sender_name_is_removed_from_the_query(self):
        r = self.extract("מה עומר אמר על אמנון?", {"query": "עומר", "senders": ["Omer Bernstein"], "from": "", "to": ""})
        self.assertEqual(r["query"], "מה אמר על אמנון?")  # the name alone left nothing: the question without the name
        r = self.extract("what did Omer say about the casino?",
                         {"query": "Omer casino", "senders": ["Omer Bernstein"], "from": "", "to": ""})
        self.assertEqual(r["query"], "casino")

    def test_unknown_and_unnamed_senders_are_dropped(self):
        r = self.extract("what did Moshe and Yoav say about pizza?",
                         {"query": "pizza", "senders": ["Moshe", "Yoav Elkayam", "Bar Ventura"], "from": "", "to": ""})
        self.assertEqual(r["senders"], ["Yoav Elkayam"])
        self.assertEqual({d["value"]: d["reason"] for d in r["dropped"]},
                         {"Moshe": "not a known participant", "Bar Ventura": "not named in the question"})

    def test_bad_date_is_dropped(self):
        r = self.extract("what was discussed last week?", {"query": "what was discussed", "senders": [],
                                                          "from": "last week", "to": "2026-11-21"})
        self.assertEqual((r["from"], r["to"]), ("", "2026-11-21"))
        self.assertEqual(r["dropped"][0]["field"], "from")

    def test_no_filters_searches_the_question_as_asked(self):
        question = "what is the plan for the party?"
        r = self.extract(question, {"query": "party plan", "senders": [], "from": "", "to": ""})
        self.assertEqual(r["query"], question)
        self.assertFalse(r["filtered"])

    def test_empty_query_falls_back_to_the_question(self):
        r = self.extract("מה נאמר ב-5 באוגוסט?", {"query": " ", "senders": [], "from": "2026-08-05", "to": "2026-08-05"})
        self.assertEqual(r["query"], "מה נאמר ב-5 באוגוסט?")
        self.assertTrue(r["filtered"])

    def test_garbage_reply(self):
        r = self.extract("hello", ["not", "a", "dict"])
        self.assertEqual((r["query"], r["senders"], r["filtered"]), ("hello", [], False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
