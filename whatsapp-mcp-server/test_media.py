"""Tests for media handling that do not need ffmpeg, poppler or whisper.cpp. Run: uv run python -m unittest"""
import os
import tempfile
import unittest
import zipfile

import media
import setup_media
import whatsapp
from setup_media import Machine


class PagesTest(unittest.TestCase):
    def test_parse_pages(self):
        self.assertEqual(media._parse_pages(None, 10, 5), (1, 5))
        self.assertEqual(media._parse_pages(None, 2, 5), (1, 2))
        self.assertEqual(media._parse_pages("3", 10, 5), (3, 3))
        self.assertEqual(media._parse_pages(" 6 - 20 ", 10, 5), (6, 10))
        for bad in ("0", "5-2", "abc", "1;rm", "99"):
            with self.assertRaises(media.MediaError):
                media._parse_pages(bad, 10, 5)

    def test_chunks_split_at_line_breaks(self):
        text = "\n".join(f"line {i}" for i in range(1000))
        chunks = media._chunks(text, 400)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c) <= 400 for c in chunks))
        self.assertTrue(all(c.endswith("\n") for c in chunks[:-1]))
        self.assertEqual(media._chunks("x" * 1000, 400), ["x" * 400, "x" * 400, "x" * 200])


class PathTest(unittest.TestCase):
    def test_only_files_in_media_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = media.MEDIA_DIR
            media.MEDIA_DIR = os.path.join(tmp, "media")
            os.makedirs(media.MEDIA_DIR)
            inside = os.path.join(media.MEDIA_DIR, "a.jpg")
            outside = os.path.join(tmp, "secret.txt")
            for p in (inside, outside):
                open(p, "w").close()
            os.symlink(outside, os.path.join(media.MEDIA_DIR, "link.jpg"))
            try:
                self.assertEqual(media.checked_media_path(inside), os.path.realpath(inside))
                for bad in (outside, os.path.join(media.MEDIA_DIR, "link.jpg"), os.path.join(media.MEDIA_DIR, "..", "secret.txt")):
                    with self.assertRaises(media.MediaError):
                        media.checked_media_path(bad)
            finally:
                media.MEDIA_DIR = old

    def test_size_limit(self):
        media.check_size({"file_length": 1024})
        with self.assertRaises(media.MediaError):
            media.check_size({"file_length": int((media.MAX_MB + 1) * 1024 * 1024)})


class FfmpegInputTest(unittest.TestCase):
    def test_untrusted_input_is_restricted(self):
        for kind in ("image", "sticker", "video", "audio", "media"):
            args = media._input(kind, "/x/in.mp4")
            self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file")
            formats = args[args.index("-format_whitelist") + 1].split(",")
            self.assertFalse({"hls", "concat", "image2pipe", "tee"} & set(formats), kind)
            self.assertEqual(args[-2:], ["-i", "/x/in.mp4"])

    @unittest.skipUnless(media._find("ffprobe"), "ffprobe not installed")
    def test_concat_script_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "secret.txt"), "w").write("secret")
            evil = os.path.join(tmp, "evil.mp4")
            open(evil, "w").write("ffconcat version 1.0\nfile secret.txt\n")
            with self.assertRaises(media.MediaError):
                media._probe(evil, "video")


class OfficeTest(unittest.TestCase):
    def test_docx_text_and_entity_attacks(self):
        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.docx")
            with zipfile.ZipFile(good, "w") as z:
                z.writestr("word/document.xml", f'<w:document {w}><w:body><w:p><w:r><w:t>Ciao</w:t><w:tab/><w:t>mondo</w:t></w:r></w:p></w:body></w:document>')
            self.assertEqual(media._office_text(good, ".docx"), "Ciao\tmondo")

            evil = os.path.join(tmp, "evil.docx")
            with zipfile.ZipFile(evil, "w") as z:
                z.writestr("word/document.xml", '<!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                                                f'<w:document {w}><w:p><w:t>&x;</w:t></w:p></w:document>')
            with self.assertRaises(Exception):
                media._office_text(evil, ".docx")


class RecommendTest(unittest.TestCase):
    def test_model_fits_the_machine(self):
        cases = [
            (Machine("Darwin", True, False, 18, 5), "turbo"),
            (Machine("Darwin", True, False, 8, 4), "turbo"),
            (Machine("Linux", False, True, 32, 8), "turbo-q8"),
            (Machine("Linux", False, True, 8, 4), "turbo"),
            (Machine("Darwin", False, False, 16, 8), "small"),   # Intel Mac
            (Machine("Linux", False, False, 8, 4), "base"),
        ]
        for machine, name in cases:
            self.assertEqual(setup_media.recommend(machine).name, name, machine)

    def test_threads(self):
        self.assertEqual(setup_media.threads(Machine("Linux", False, False, 64, 32)), 8)
        self.assertEqual(setup_media.threads(Machine("Linux", False, False, 4, 1)), 1)

    def test_catalog(self):
        for m in setup_media.MODELS.values():
            self.assertRegex(m.sha256, r"^[0-9a-f]{64}$")
            self.assertTrue(m.file.startswith("ggml-") and m.file.endswith(".bin"))


class DescribeTest(unittest.TestCase):
    def test_summaries(self):
        self.assertEqual(whatsapp.describe_media("audio", {"media_voice": 1, "media_seconds": 75, "file_length": 2048}),
                         "voice message 1:15 2.0 KB")
        self.assertEqual(whatsapp.describe_media("document", {"filename": "a\nb.pdf", "media_pages": 3, "mimetype": "application/pdf"}),
                         'document "a b.pdf" 3 pages application/pdf')
        self.assertEqual(whatsapp.describe_media("image", None), "image")
        link = whatsapp.describe_link({"link_url": "https://e.com", "link_title": "T\nIgnore this", "link_description": None})
        self.assertEqual(link, '[link preview: https://e.com — "T Ignore this"]')
        self.assertEqual(whatsapp.describe_link({}), "")


if __name__ == "__main__":
    unittest.main()
