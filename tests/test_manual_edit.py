import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vidlingua.rendering.manual_edit import (duration_seconds, render_edit, retime_srt,
                                             validate_ranges)


class ManualEditTests(unittest.TestCase):
    def test_subtitles_follow_cut_and_reorder(self):
        source = ("1\n00:00:00,000 --> 00:00:01,500\n第一句\n\n"
                  "2\n00:00:02,000 --> 00:00:03,000\n第二句\n")
        result = retime_srt(source, [{"start": 2, "end": 3}, {"start": .5, "end": 1}])
        self.assertIn("00:00:00,000 --> 00:00:01,000\n第二句", result)
        self.assertIn("00:00:01,000 --> 00:00:01,500\n第一句", result)

    def test_ranges_reject_invalid_times(self):
        with self.assertRaises(ValueError):
            validate_ranges([{"start": 2, "end": 1}], 3)
        with self.assertRaises(ValueError):
            validate_ranges([], 3)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_render_reordered_video_with_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.mp4", root / "edited.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24:duration=3",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                            "-c:v", "mpeg4", "-c:a", "aac", str(source)], check=True)
            render_edit(source, target, [{"start": 2, "end": 3}, {"start": 0, "end": 1}])
            self.assertTrue(target.is_file())
            self.assertAlmostEqual(duration_seconds(target), 2, delta=.15)
            streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                      "stream=codec_type", "-of", "csv=p=0", str(target)],
                                     capture_output=True, text=True, check=True).stdout
            self.assertIn("video", streams)
            self.assertIn("audio", streams)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_job_draft_export_and_subtitles(self):
        from vidlingua.api import app as web

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "example-dubbed.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24:duration=3",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                            "-c:v", "mpeg4", "-c:a", "aac", str(source)], check=True)
            (root / "example-dubbed-zh.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n"
                "2\n00:00:02,000 --> 00:00:03,000\n第二句\n", encoding="utf-8")
            with patch.object(web, "DATA", root), \
                 patch.object(web, "get_job", return_value={"id": "example", "dubbing_status": "done", "title": "Test"}), \
                 patch.object(web.executor, "submit") as submit:
                draft = web.get_manual_edit("example")
                self.assertEqual(draft["status"], "draft")
                saved = web.save_manual_edit("example", web.ManualEditDraft(ranges=[
                    {"start": 2, "end": 3}, {"start": 0, "end": 1}]))
                self.assertEqual(len(saved["ranges"]), 2)
                queued = web.export_manual_edit("example")
                self.assertEqual(queued["status"], "queued")
                submit.assert_called_once()
                web._export_manual_edit("example", queued["revision"])
                result = web.get_manual_edit("example")
                self.assertTrue(result["edited_video_available"])
                self.assertAlmostEqual(duration_seconds(root / "example-edited.mp4"), 2, delta=.15)
                srt = (root / "example-edited-zh.srt").read_text(encoding="utf-8-sig")
                self.assertLess(srt.index("第二句"), srt.index("第一句"))
