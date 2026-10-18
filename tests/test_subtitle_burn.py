"""Subtitle burn settings checks."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from vidlingua.rendering import subtitles as subtitle_burn
from vidlingua import web_app


class SubtitleBurnTests(unittest.TestCase):
    def test_settings_persist_and_reject_invalid_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "subtitles.json"
            expected = subtitle_burn.save_config(path, {"enabled": True, "mode": "bilingual",
                                                        "font_size": 30})
            self.assertEqual(subtitle_burn.load_config(path), expected)
            self.assertEqual(subtitle_burn.save_config(path, {"mode": "bilingual", "font_size": 5})["font_size"], 5)
            with self.assertRaises(ValueError):
                subtitle_burn.save_config(path, {"font_size": 4})
            with patch.object(web_app, "SUBTITLE_CONFIG", path):
                self.assertEqual(web_app.subtitle_config()["mode"], "bilingual")
                with self.assertRaises(HTTPException):
                    web_app.configure_subtitles(web_app.SubtitleConfigEdit(font_size=80))
