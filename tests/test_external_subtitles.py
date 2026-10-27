"""Same-named batch subtitle import checks."""

import json
import tempfile
import unittest
from pathlib import Path

from vidlingua.pipeline.external_subtitles import (import_subtitle_transcript,
                                                   matching_subtitle, parse_subtitles)
from vidlingua.pipeline.preparation import make_segments


class ExternalSubtitleTests(unittest.TestCase):
    def test_srt_import_skips_word_alignment_review_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Lesson.mp4"
            video.write_bytes(b"video")
            subtitle = root / "Lesson.srt"
            subtitle.write_text("1\n00:00:01,000 --> 00:00:03,500\nHello <i>world</i>.\n\n"
                                "2\n00:00:04,000 --> 00:00:05,000\nNext line.\n", encoding="utf-8")
            self.assertEqual(matching_subtitle(video), subtitle)
            transcript = root / "transcript" / "asr_audio.json"
            self.assertFalse(import_subtitle_transcript(subtitle, transcript))
            self.assertTrue(import_subtitle_transcript(subtitle, transcript))
            self.assertEqual(json.loads(transcript.read_text(encoding="utf-8"))["language"], "en")
            segments = make_segments(transcript, 10)
            self.assertEqual([item["edited_text"] for item in segments], ["Hello world.", "Next line."])
            self.assertEqual([item["flags"] for item in segments], [[], []])

    def test_vtt_and_non_english_subtitle_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitle = root / "Lesson.vtt"
            subtitle.write_text("WEBVTT\n\n00:01.000 --> 00:02.500 align:start\nA test line.\n", encoding="utf-8")
            self.assertEqual(parse_subtitles(subtitle)[0]["start"], 1.0)
            subtitle.write_text("WEBVTT\n\n00:01.000 --> 00:02.500\n这是一句中文字幕。\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不是英文"):
                parse_subtitles(subtitle)


if __name__ == "__main__":
    unittest.main()
