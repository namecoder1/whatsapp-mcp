"""Tests for reading the message store, search and downloads, on a temporary database. Run: uv run python -m unittest"""
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from unittest import mock

import media
import whatsapp


def make_store(tmp: str) -> str:
    """A messages.db like the bridge creates, with a few chats and messages."""
    path = os.path.join(tmp, "messages.db")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
        CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN,
            media_type TEXT, filename TEXT, url TEXT, media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
            mimetype TEXT, media_seconds INTEGER, media_width INTEGER, media_height INTEGER, media_pages INTEGER,
            media_voice BOOLEAN, link_url TEXT, link_title TEXT, link_description TEXT, direct_path TEXT, sender_jid TEXT,
            PRIMARY KEY (id, chat_jid));
    """)
    now = datetime.now().astimezone()
    ts = lambda days: (now - timedelta(days=days)).isoformat(sep=" ", timespec="seconds")
    db.executemany("INSERT INTO chats VALUES (?, ?, ?)", [
        ("393331234567@s.whatsapp.net", "Marco Rossi", ts(0)),
        ("393339999999@s.whatsapp.net", "Marco Bianchi", ts(1)),
        ("120363000000000001@g.us", "Calcetto Giovedì", ts(2)),
        ("1111@lid", "~Niccolò", ts(3)),
    ])
    rows = [
        ("m1", "393331234567@s.whatsapp.net", "393331234567", "Ti mando la fattura di novembre", ts(0.5), 0, None, None, None, None),
        ("m2", "393331234567@s.whatsapp.net", "393331234567", "", ts(0.4), 0, "document", "Fattura_11.pdf", 3, 120000),
        ("m3", "393331234567@s.whatsapp.net", "me", "grazie!", ts(0.3), 1, None, None, None, None),
        ("m4", "393339999999@s.whatsapp.net", "393339999999", "Ci vediamo giovedì?", ts(1), 0, None, None, None, None),
        ("m5", "120363000000000001@g.us", "1111@lid", "Campo prenotato alle 21", ts(2), 0, None, None, None, None),
        ("m6", "120363000000000001@g.us", "393331234567", "", ts(20), 0, "document", "Regolamento.docx", None, 9000),
    ]
    db.executemany("""INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename,
                      media_pages, file_length) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    db.commit()
    db.close()
    return path


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = self.tmp.name
        self.patches = [mock.patch.multiple(
            whatsapp, MESSAGES_DB_PATH=make_store(tmp), SEARCH_DB_PATH=os.path.join(tmp, "search.db"),
            SESSION_DB_PATH=os.path.join(tmp, "missing.db"), MEDIA_CACHE_DB_PATH=os.path.join(tmp, "cache.db"),
            MEDIA_DIR=os.path.join(tmp, "media"), DOWNLOAD_DIR=os.path.join(tmp, "Downloads"))]
        for p in self.patches:
            p.start()
        whatsapp.Names._cache = None

    def tearDown(self):
        for p in self.patches:
            p.stop()
        whatsapp.Names._cache = None
        self.tmp.cleanup()

    def test_resolve_chat(self):
        conn = whatsapp._connect()
        try:
            self.assertEqual(whatsapp.resolve_chat(conn, "marco rossi")[0], "393331234567@s.whatsapp.net")
            self.assertEqual(whatsapp.resolve_chat(conn, "calcetto giovedi")[0], "120363000000000001@g.us")  # no accent
            self.assertEqual(whatsapp.resolve_chat(conn, "+39 333 123 4567")[0], "393331234567@s.whatsapp.net")
            self.assertEqual(whatsapp.resolve_chat(conn, "niccolo")[0], "1111@lid")
            with self.assertRaisesRegex(ValueError, "Several chats"):
                whatsapp.resolve_chat(conn, "marco")
            with self.assertRaisesRegex(ValueError, "No chat"):
                whatsapp.resolve_chat(conn, "nessuno")
            with self.assertRaisesRegex(ValueError, "No chat with JID"):
                whatsapp.resolve_chat(conn, "1@g.us")
        finally:
            conn.close()

    def test_read_chat(self):
        out = whatsapp.read_chat("Marco Rossi")
        self.assertIn("messages 1-3 of 3", out)
        lines = out.splitlines()
        self.assertTrue(lines[1].startswith("── "))
        # Oldest first, attachments with their ID, my messages as "Me"
        self.assertLess(out.index("fattura di novembre"), out.index("grazie!"))
        self.assertIn('[document "Fattura_11.pdf" 3 pages 117.2 KB · id m2]', out)
        self.assertIn("Me: grazie!", out)
        self.assertIn("Older messages: page=1", whatsapp.read_chat("Marco Rossi", limit=2))
        self.assertIn("No messages", whatsapp.read_chat("Marco Rossi", until="3d"))

    def test_search(self):
        out = whatsapp.search_messages("fattur")
        self.assertIn("m2", out)  # file name
        self.assertIn("fattura di novembre", out)
        self.assertIn("Campo prenotato", whatsapp.search_messages("PRENOTATO", chat="calcetto"))
        self.assertIn("No messages", whatsapp.search_messages('"novembre fattura"'))  # phrase order matters
        self.assertIn("Ci vediamo", whatsapp.search_messages("giovedi"))  # accents ignored
        # Text of documents opened before is searchable
        cache = sqlite3.connect(whatsapp.MEDIA_CACHE_DB_PATH)
        cache.execute("CREATE TABLE analysis (message_id TEXT, chat_jid TEXT, kind TEXT, text TEXT, created_at REAL)")
        cache.execute("INSERT INTO analysis VALUES ('m6', '120363000000000001@g.us', 'text', 'Quota annuale 50 euro', 1)")
        cache.commit()
        cache.close()
        out = whatsapp.search_messages("quota annuale")
        self.assertIn("match in the document's text", out)
        with self.assertRaises(ValueError):
            whatsapp.search_messages("  ?! ")

    def test_attachments_and_activity(self):
        out = whatsapp.list_attachments(kind="document")
        self.assertIn("Fattura_11.pdf", out)
        self.assertIn("on WhatsApp's servers", out)
        self.assertIn("expired on WhatsApp's servers", out)  # Regolamento.docx is 20 days old
        self.assertNotIn("Regolamento", whatsapp.list_attachments(since="1w"))
        activity = whatsapp.recent_activity("1d")
        self.assertIn("Marco Rossi", activity)
        self.assertNotIn("awaiting your reply", activity.split("Marco Rossi")[1].split("\n")[0])  # I answered last
        self.assertIn("~Niccolò", whatsapp.list_chats("nicco"))

    def test_context_marks_the_message(self):
        out = whatsapp.get_message_context("m2", before=1, after=1)
        self.assertIn("▶ ", out)
        self.assertIn("fattura di novembre", out)
        self.assertIn("grazie!", out)

    def test_download_errors_say_what_to_do(self):
        answer = (500, json.dumps({"success": False, "error_code": "not_on_phone", "message": "x"}))
        with mock.patch("bridge.call", return_value=answer):
            with self.assertRaisesRegex(whatsapp.DownloadError, "Ask the sender to send it again"):
                whatsapp.fetch_media("m2", "393331234567@s.whatsapp.net")

    def test_save_media_never_overwrites(self):
        src_dir = os.path.join(whatsapp.MEDIA_DIR, "chat")
        os.makedirs(src_dir)
        src = os.path.join(src_dir, "m2_Fattura_11.pdf")
        with open(src, "w") as f:
            f.write("pdf")
        with mock.patch.object(whatsapp, "fetch_media", return_value=src):
            first = whatsapp.save_media("m2", "393331234567@s.whatsapp.net")
            second = whatsapp.save_media("m2", "393331234567@s.whatsapp.net")
        self.assertEqual(os.path.basename(first), "Fattura_11.pdf")
        self.assertEqual(os.path.basename(second), "Fattura_11 (2).pdf")
        outside = os.path.join(self.tmp.name, "secret.txt")
        with open(outside, "w") as f:
            f.write("x")
        with mock.patch.object(whatsapp, "fetch_media", return_value=outside):
            with self.assertRaises(whatsapp.DownloadError):
                whatsapp.save_media("m2", "393331234567@s.whatsapp.net")


class HelpersTest(unittest.TestCase):
    def test_parse_time(self):
        now = datetime(2026, 11, 26, 15, 30)
        self.assertEqual(whatsapp.parse_time("today", now=now), datetime(2026, 11, 26))
        self.assertEqual(whatsapp.parse_time("yesterday", now=now), datetime(2026, 11, 25))
        self.assertEqual(whatsapp.parse_time("yesterday", end=True, now=now), datetime(2026, 11, 26))
        self.assertEqual(whatsapp.parse_time("3d", now=now), datetime(2026, 11, 23, 15, 30))
        self.assertEqual(whatsapp.parse_time("12 hours ago", now=now), datetime(2026, 11, 26, 3, 30))
        self.assertEqual(whatsapp.parse_time("2026-11-20", end=True, now=now), datetime(2026, 11, 21))
        self.assertEqual(whatsapp.parse_time("2026-11-20T10:00", end=True, now=now), datetime(2026, 11, 20, 10))
        self.assertIsNone(whatsapp.parse_time(None))
        with self.assertRaises(ValueError):
            whatsapp.parse_time("next friday")

    def test_fts_query_is_always_safe(self):
        self.assertEqual(whatsapp._fts_query("fattura marzo"), '"fattura"* "marzo"*')
        self.assertEqual(whatsapp._fts_query('"codice pratica" 2025'), '"codice pratica" "2025"*')
        self.assertEqual(whatsapp._fts_query('a" OR NEAR(x'), '"a"* "OR"* "NEAR"* "x"*')
        self.assertEqual(whatsapp._fts_query("?!"), "")

    def test_safe_filename_matches_the_bridge(self):
        # Same cases as TestSafeFilename in security_test.go
        cases = {"../../../.zshrc": "zshrc", "..\\..\\evil.exe": "evil.exe", "/etc/passwd": "passwd",
                 "fattura marzo.pdf": "fattura marzo.pdf", "..": "fallback", "": "fallback",
                 "rapporto\x00.pdf": "rapporto_.pdf", "s.whatsapp.net@123_45": "s.whatsapp.net_123_45"}
        for name, want in cases.items():
            self.assertEqual(whatsapp.safe_filename(name, "fallback"), want, name)


class DocumentFormatsTest(unittest.TestCase):
    def _zip(self, tmp, name, files):
        path = os.path.join(tmp, name)
        with zipfile.ZipFile(path, "w") as z:
            for member, data in files.items():
                z.writestr(member, data)
        return path

    def test_xlsx(self):
        S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        with tempfile.TemporaryDirectory() as tmp:
            path = self._zip(tmp, "a.xlsx", {
                "xl/workbook.xml": f'<workbook {S}><sheets><sheet name="Costi"/></sheets></workbook>',
                "xl/sharedStrings.xml": f"<sst {S}><si><t>Voce</t></si><si><t>Importo</t></si><si><t>Affitto</t></si></sst>",
                "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData>'
                    '<row><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                    '<row><c r="A2" t="s"><v>2</v></c><c r="C2"><v>800</v></c></row>'
                    '</sheetData></worksheet>'})
            self.assertEqual(media._office_text(path, ".xlsx"), "--- Sheet 1: Costi ---\nVoce\tImporto\nAffitto\t\t800")

    def test_opendocument(self):
        T = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
        TB = 'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
        with tempfile.TemporaryDirectory() as tmp:
            odt = self._zip(tmp, "a.odt", {"content.xml": f'<doc {T}><text:h>Titolo</text:h><text:p>Ciao <text:span>mondo</text:span></text:p></doc>'})
            self.assertEqual(media._odf_text(odt, ".odt"), "Titolo\nCiao mondo")
            ods = self._zip(tmp, "a.ods", {"content.xml": f'<doc {T} {TB}><table:table table:name="Foglio1"><table:table-row>'
                '<table:table-cell><text:p>a</text:p></table:table-cell>'
                '<table:table-cell table:number-columns-repeated="1000000"/>'
                '<table:table-cell><text:p>b</text:p></table:table-cell></table:table-row></table:table></doc>'})
            text = media._odf_text(ods, ".ods")
            self.assertTrue(text.startswith("--- Sheet 1: Foglio1 ---\na\t"))
            self.assertLess(len(text), 200)  # the repeat count is capped

    def test_cached_text_needs_no_file(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media, "CACHE_DB", os.path.join(tmp, "c.db")):
            media._store("m1", "c", "text", "page one " + "x" * 50 + "\fpage two " + "y" * 50 + "\f")

            def no_file():
                raise AssertionError("the file must not be fetched when its text is cached")

            out = media.inspect(no_file, {"media_type": "document", "filename": "a.pdf"}, "m1", "c", "low", None)
            self.assertIn("page one", out[1].text)
            self.assertIn("2 page(s)", out[1].text)


if __name__ == "__main__":
    unittest.main()
