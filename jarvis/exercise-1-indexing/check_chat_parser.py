"""Tests for the WhatsApp export parser and the message-aligned chunks (no network, no database).

  python check_chat_parser.py
"""
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path

import chat_parser as cp

TZ = cp.chat_timezone("Asia/Jerusalem")
UTC = timezone.utc

SAMPLE = """# WhatsApp Chat Export: Team chat
Export date: 12 בנובמבר 2026 בשעה 15:20

---

## 30 ביולי 2026

[11:19] **Idan Aviad:** [Forwarded] first line of a long message

second paragraph of the same message

[11:23] **Yael Rothschild :** short one

[11:24] **‎Omer Bernstein‏:**
> _Yael Rothschild : short one_
reply text

---

## 12 בנובמבר 2026

[9:30] **מחליף של בר ענבר:** [Sticker]

[9:31] **You:** [Contact: BEGIN:VCARD
VERSION:3.0
FN:Some Person
item1.TEL;waid=972500000000:+972 50-000-0000
END:VCARD]

[10:02] **~ Dana:** **Poll: when?**
- 22.9
- 23.9
"""


class ParseMessages(unittest.TestCase):
    def setUp(self):
        self.chat = cp.parse_chat(SAMPLE, TZ)
        self.messages = self.chat["messages"]

    def test_title_and_count(self):
        self.assertEqual(self.chat["title"], "Team chat")
        self.assertEqual(len(self.messages), 6)

    def test_multi_line_message_keeps_blank_line_and_drops_separator(self):
        self.assertEqual(self.messages[0]["text"],
                         "[Forwarded] first line of a long message\n\nsecond paragraph of the same message")
        self.assertNotIn("---", self.messages[2]["text"])  # the day separator is not part of the message

    def test_unusual_names_are_canonical(self):
        senders = [m["sender"] for m in self.messages]
        self.assertEqual(senders, ["Idan Aviad", "Yael Rothschild", "Omer Bernstein", "מחליף של בר ענבר", "You", "Dana"])

    def test_reply_quote_is_structured(self):
        reply = self.messages[2]
        self.assertEqual(reply["text"], "reply text")
        self.assertEqual(reply["reply_to"], {"sender": "Yael Rothschild", "text": "short one"})
        self.assertIsNone(self.messages[1]["reply_to"])

    def test_times_are_utc_with_daylight_saving(self):
        self.assertEqual(self.messages[0]["ts"], datetime(2026, 7, 30, 8, 19, tzinfo=UTC))  # IDT, UTC+3
        self.assertEqual(self.messages[3]["ts"], datetime(2026, 11, 12, 7, 30, tzinfo=UTC))  # IST, UTC+2

    def test_vcard_and_poll_stay_one_message(self):
        self.assertIn("END:VCARD]", self.messages[4]["text"])
        self.assertEqual(self.messages[5]["text"], "**Poll: when?**\n- 22.9\n- 23.9")

    def test_offsets_point_at_the_source(self):
        for m in self.messages:
            self.assertTrue(SAMPLE[m["start"]:m["end"]].startswith("["))
            self.assertFalse(SAMPLE[m["start"]:m["end"]].endswith("\n"))

    def test_message_before_date_header_is_an_error(self):
        with self.assertRaises(ValueError):
            cp.parse_chat("# WhatsApp Chat Export: x\n[10:00] **A:** hi\n", TZ)

    def test_timezone_is_configurable(self):
        utc_chat = cp.parse_chat(SAMPLE, cp.chat_timezone("UTC"))
        self.assertEqual(utc_chat["messages"][0]["ts"], datetime(2026, 7, 30, 11, 19, tzinfo=UTC))

    def test_detects_export(self):
        self.assertTrue(cp.is_chat_export(SAMPLE))
        self.assertFalse(cp.is_chat_export("# Team meeting\n\nnotes"))


class DateFormats(unittest.TestCase):
    def test_formats(self):
        for text in ("30 ביולי 2026", "30 יולי 2026", "30 July 2026", "30 Jul 2026", "July 30, 2026",
                     "2026-07-30", "30/07/2026", "30.07.2026"):
            self.assertEqual(cp.parse_day(text), (2026, 7, 30), text)

    def test_not_a_date(self):
        for text in ("Summary", "31 בפברואר 2026", "Projects 2026"):
            self.assertIsNone(cp.parse_day(text), text)

    def test_twelve_hour_clock_and_seconds(self):
        chat = cp.parse_chat("## July 30, 2026\n[1:05 PM] **A:** x\n[12:00:30 AM] **B:** y\n", TZ)
        self.assertEqual([m["ts"] for m in chat["messages"]],
                         [datetime(2026, 7, 30, 10, 5, tzinfo=UTC), datetime(2026, 7, 29, 21, 0, 30, tzinfo=UTC)])

    def test_english_month_names_in_day_headers(self):
        chat = cp.parse_chat("## 5 August 2026\n[10:00] **A:** x\n", TZ)
        self.assertEqual(chat["messages"][0]["local"], datetime(2026, 8, 5, 10, 0))


class Views(unittest.TestCase):
    def setUp(self):
        self.title, self.chunks = cp.build_chunks("chat.md", SAMPLE, TZ, max_chars=900)

    def test_display_text_has_sender_and_time(self):
        lines = self.chunks[0]["display_text"].split("\n")
        self.assertTrue(lines[0].startswith("Idan Aviad [2026-07-30 11:19]: "))
        self.assertIn('Omer Bernstein [2026-07-30 11:24]: (replying to Yael Rothschild: "short one") reply text',
                      self.chunks[0]["display_text"])

    def test_embed_text_has_no_names_or_times(self):
        embed = "\n".join(c["embed_text"] for c in self.chunks)
        for name in ("Idan", "Yael", "Omer", "Dana", "מחליף", "11:19", "2026-07-30"):
            self.assertNotIn(name, embed)
        self.assertIn("first line of a long message", embed)
        self.assertIn("> short one\nreply text", embed)  # a reply keeps the quoted words, without the name
        self.assertNotIn("[Sticker]", embed)
        self.assertNotIn("[Forwarded]", embed)
        self.assertIn("Contact: Some Person", embed)
        self.assertNotIn("972", embed)

    def test_participants_and_range(self):
        chunk = self.chunks[0]
        self.assertEqual(chunk["participants"][:3], ["Idan Aviad", "Yael Rothschild", "Omer Bernstein"])
        self.assertEqual(chunk["start_ts"], chunk["messages"][0]["ts"])
        self.assertEqual(chunk["end_ts"], chunk["messages"][-1]["ts"])


class Chunks(unittest.TestCase):
    def make(self, sizes):
        lines = ["## 1 August 2026"] + [f"[10:{i:02d}] **P{i % 3}:** " + "x" * size for i, size in enumerate(sizes)]
        return cp.parse_chat("\n".join(lines) + "\n", TZ)["messages"]

    def test_never_splits_and_keeps_order(self):
        messages = self.make([50, 300, 10, 700, 80, 2000, 5, 5])
        groups = cp.chunk_messages(messages, max_chars=400)
        self.assertEqual([m for g in groups for m in g], messages)  # every message once, in order
        for group in groups:
            text = "\n".join(cp.display_line(m) for m in group)
            self.assertTrue(len(text) <= 400 or len(group) == 1)  # only a single oversize message may exceed

    def test_oversize_message_is_its_own_chunk(self):
        messages = self.make([10, 2000, 10])
        groups = cp.chunk_messages(messages, max_chars=400)
        self.assertEqual([len(g) for g in groups], [1, 1, 1])

    def test_merges_up_to_the_limit(self):
        messages = self.make([10] * 20)
        groups = cp.chunk_messages(messages, max_chars=900)
        self.assertEqual(len(groups), 1)

    def test_index_records_match_the_index_contract(self):
        document = {"document_id": "chat", "version": "1", "status": "current", "updated": "2026-11-12",
                    "source": "vault/chat.md", "title": "WhatsApp Chat Export: Team chat", "path": "chat.md",
                    "body": SAMPLE, "document_sha256": "x", "kind": "chat"}
        records = cp.index_records(document, 900, TZ)
        _, chunks = cp.build_chunks("chat.md", SAMPLE, TZ, 900)
        self.assertEqual([r["chunk_id"] for r in records], [f"chat:chat.md:{i}" for i in range(len(records))])
        for record, chunk in zip(records, chunks):
            self.assertEqual(record["kind"], "chat")
            self.assertEqual(record["text"], chunk["display_text"])  # what the answer model reads
            self.assertEqual(record["embedding_text"], chunk["embed_text"])  # what the embedding is computed from
            self.assertEqual(SAMPLE[record["start"]:record["end"]][:1], "[")
            self.assertTrue(record["metadata"]["start_ts"].endswith("Z"))
            self.assertEqual(cp.from_iso(record["chat"]["start_ts"]).tzinfo, UTC)


class RealExport(unittest.TestCase):  # the vault's chat.md, when it is on this computer
    PATH = Path(os.environ.get("JARVIS_VAULT") or Path.home() / "Documents" / "Obsidian Vault") / "chat.md"

    @unittest.skipUnless(PATH.exists(), "no chat.md in the vault")
    def test_every_message_is_parsed_and_chunked_once(self):
        text = self.PATH.read_text(encoding="utf-8-sig")
        headers = sum(1 for line in text.splitlines() if cp.MESSAGE.match(line))
        _, chunks = cp.build_chunks("chat.md", text, TZ)
        self.assertEqual(sum(len(c["messages"]) for c in chunks), headers)
        self.assertTrue(all(len(c["display_text"]) <= 900 or len(c["messages"]) == 1 for c in chunks))
        self.assertTrue(all(c["start_ts"] <= c["end_ts"] for c in chunks))


if __name__ == "__main__":
    unittest.main(verbosity=2)
