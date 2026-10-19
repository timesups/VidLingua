"""YouTube URL and download handoff checks without network access."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vidlingua.pipeline import youtube


class YouTubeTests(unittest.TestCase):
    def test_canonical_single_video_urls(self):
        for value in (
            "https://www.youtube.com/watch?v=abcdefghijk&t=12",
            "https://youtu.be/abcdefghijk?si=example",
            "https://youtube.com/shorts/abcdefghijk",
            "https://m.youtube.com/live/abcdefghijk",
        ):
            self.assertEqual(youtube.canonical_youtube_url(value),
                             ("https://www.youtube.com/watch?v=abcdefghijk", "abcdefghijk"))

    def test_rejects_non_video_or_untrusted_urls(self):
        for value in (
            "http://youtube.com/watch?v=abcdefghijk",
            "https://youtube.com.evil.test/watch?v=abcdefghijk",
            "https://youtube.com@evil.test/watch?v=abcdefghijk",
            "https://user@youtube.com/watch?v=abcdefghijk",
            "https://youtube.com:444/watch?v=abcdefghijk",
            "https://youtube.com/playlist?list=abcdefghijk",
            "https://youtu.be/abcdefghijk/extra",
            "https://youtube.com/watch?v=abcdefghijk\nHost: evil.test",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                youtube.canonical_youtube_url(value)

    def test_download_verifies_media_before_publishing(self):
        seen = []

        class FakeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, url, download):
                seen.append((url, download))
                self.options["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 50,
                                                      "total_bytes": 100})
                Path(self.options["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"video")
                return {"id": "abcdefghijk", "title": "Example lesson"}

        with tempfile.TemporaryDirectory() as directory, \
             patch("yt_dlp.YoutubeDL", FakeDL), \
             patch.object(youtube, "probe", return_value={"streams": [
                 {"codec_type": "video"}, {"codec_type": "audio"}]}):
            root = Path(directory)
            target = root / "uploads" / "youtube-abcdefghijk.mp4"
            log = root / "job.log"
            result = youtube.download_youtube("https://youtu.be/abcdefghijk", target, log,
                                              root / "config.json", root / "cookies.txt")
            self.assertEqual(result["title"], "Example lesson")
            self.assertEqual(target.read_bytes(), b"video")
            self.assertIn("YouTube: 50%", log.read_text(encoding="utf-8"))
            self.assertEqual(seen, [("https://www.youtube.com/watch?v=abcdefghijk", True)])

    def test_jpeg_thumbnail_is_saved_beside_the_video(self):
        jpeg = b"\xff\xd8\xff\xd9"
        response = type("Response", (), {"content": jpeg, "raise_for_status": lambda self: None})()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(youtube, "_thumbnail_session") as session:
            session.return_value.get.return_value = response
            cover = Path(directory) / "cover.jpg"
            saved = youtube.save_youtube_cover(
                {"thumbnails": [{"url": "https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg", "width": 1280}]},
                cover, Path(directory) / "config.json")
            self.assertTrue(saved)
            self.assertEqual(cover.read_bytes(), jpeg)
            self.assertFalse(youtube.save_youtube_cover({}, Path(directory) / "missing.jpg",
                                                        Path(directory) / "missing.json"))

    def test_saved_proxy_and_cookies_are_used_by_downloader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "youtube.json"
            cookies = root / "cookies.txt"
            youtube.save_config(config, {"proxy_mode": "local", "proxy_port": "7890"})
            youtube.save_cookies(cookies, "# Netscape HTTP Cookie File\n"
                                 ".youtube.com\tTRUE\t/\tTRUE\t0\tTEST\tsecret\n")
            options = youtube.ydl_network_options(config, cookies)
            self.assertEqual(options["proxy"], "http://127.0.0.1:7890")
            self.assertEqual(options["cookiefile"], str(cookies))
            youtube.save_config(config, {"proxy_mode": "direct", "proxy_port": ""})
            self.assertEqual(youtube.ydl_network_options(config, root / "missing.txt")["proxy"], "")
            with self.assertRaises(ValueError):
                youtube.save_config(config, {"proxy_mode": "local", "proxy_port": "70000"})
            with self.assertRaises(ValueError):
                youtube.save_cookies(cookies, '{"cookies":[] }')
            self.assertIn("TEST\tsecret", cookies.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
