"""Watermark settings and rendering checks."""

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from unittest.mock import patch

import watermark
import web_app


class WatermarkTests(unittest.TestCase):
    def test_settings_persist_and_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watermark.json"
            config = watermark.save_config(path, {"enabled": True, "text": "课程", "opacity": .5})
            self.assertEqual(watermark.load_config(path), config)
            with self.assertRaises(ValueError):
                watermark.save_config(path, {"enabled": True, "text": ""})
            with patch.object(web_app, "WATERMARK_CONFIG", path):
                self.assertEqual(web_app.watermark_config()["text"], "课程")
                with self.assertRaises(HTTPException):
                    web_app.configure_watermark(web_app.WatermarkConfigEdit(
                        enabled=True, text="", position="top_right", opacity=.45))
