"""Tests for the memes MCP server's safety checks and Hebrew layout. Run: python mcp-servers/meme-mcp/check_server.py

No network: these test the input checks, the URL escaping and the right-to-left ordering directly.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402


class Safety(unittest.TestCase):
    def test_template_ids_cannot_leave_the_path(self):
        for bad in ("../templates", "drake/../../x", "drake?x=1", "http://evil", "", "a" * 60, "dr ake"):
            with self.assertRaises(ValueError, msg=bad):
                server._template_id(bad)
        self.assertEqual(server._template_id(" Drake "), "drake")

    def test_text_is_escaped_not_pasted(self):  # the audited original put text into the URL as-is
        self.assertEqual(server._encode("writing tests?"), "writing_tests~q")
        self.assertEqual(server._encode("a/b & c #1 100%"), "a~sb_~a_c_~h1_100~p")
        self.assertEqual(server._encode("snake_case-name"), "snake__case--name")
        self.assertEqual(server._encode(""), "_")
        self.assertNotIn("/", server._encode("../../admin"))
        self.assertNotIn("?", server._encode("x?style=https://evil"))

    def test_options_are_names_not_urls(self):
        for name, value in (("style", "https://evil.example/x.png"), ("font", "../../etc/passwd"), ("style", "a b")):
            with self.assertRaises(ValueError):
                server._choice(value, name)
        with self.assertRaises(ValueError):
            server._choice("php", "extension", server.EXTENSIONS)
        self.assertEqual(server._choice("PNG", "extension", server.EXTENSIONS), "png")
        self.assertIsNone(server._choice("", "font"))

    def test_backgrounds_must_be_public(self):  # no server-side request forgery into this machine or the LAN
        for bad in ("http://localhost:5000/admin", "http://127.0.0.1/x.png", "http://169.254.169.254/latest/meta-data",
                    "http://192.168.1.1/x.png", "http://10.0.0.5/x.png", "http://[::1]/x.png", "file:///C:/Windows/win.ini",
                    "ftp://example.com/x.png", "https://user:pw@example.com/x.png", "javascript:alert(1)"):
            with self.assertRaises(ValueError, msg=bad):
                server._public_url(bad)
        self.assertEqual(server._public_url("https://8.8.8.8/x.png"), "https://8.8.8.8/x.png")

    def test_text_lines_are_bounded(self):
        self.assertEqual(server._lines("one"), ["one"])
        self.assertEqual(len(server._lines(["x"] * 50)), server.MAX_LINES)
        self.assertEqual(len(server._lines(["y" * 999])[0]), server.MAX_LINE_CHARS)
        for bad in ([], ["", "  "], 42, {"a": 1}):
            with self.assertRaises(ValueError):
                server._lines(bad)


class Hebrew(unittest.TestCase):
    def test_right_to_left_drawing_order(self):
        self.assertEqual(server.visual_order("שלום עולם"), "םלוע םולש")
        self.assertEqual(server.visual_order("באג של Cedar"), "Cedar לש גאב")  # English keeps its order
        self.assertEqual(server.visual_order("ב-17:00?"), "?17:00-ב")
        self.assertEqual(server.visual_order("(בדיקה)"), "(הקידב)")  # brackets mirrored
        self.assertEqual(server.visual_order("plain English"), "plain English")


if __name__ == "__main__":
    unittest.main(verbosity=2)
