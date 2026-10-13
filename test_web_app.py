"""Regression checks for local job creation."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import UploadFile

import web_app


class JobCreationTests(unittest.TestCase):
    def test_create_job_after_translation_schema_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            uploads.mkdir()
            with patch.object(web_app, "DB", root / "jobs.sqlite3"), patch.object(web_app, "UPLOADS", uploads):
                web_app.initialize()
                upload = UploadFile(filename="course.mp4", file=io.BytesIO(b"video data"))
                job = web_app.create_job(upload, "课程", "Blender", "shader = 着色器")
                self.assertEqual(job["status"], "created")
                self.assertEqual(job["translation_status"], "not_started")
                self.assertEqual(Path(job["upload_path"]).read_bytes(), b"video data")


if __name__ == "__main__":
    unittest.main()
