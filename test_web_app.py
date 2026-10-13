"""Regression checks for local job creation."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import UploadFile

import web_app


class JobCreationTests(unittest.TestCase):
    def test_processed_video_continues_into_automatic_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "transcript_vocals").mkdir(parents=True)
            (run / "manifest.json").write_text(
                json.dumps({"media": {"duration_seconds": 2}}), encoding="utf-8")
            (run / "transcript_vocals" / "asr_vocals.json").write_text("{}", encoding="utf-8")
            with patch.object(web_app, "DATA", root), \
                 patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "import_media", return_value=run), \
                 patch.object(web_app.subprocess, "run", return_value=Mock(returncode=0)), \
                 patch.object(web_app, "translate_then_dub") as finish:
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "Test", "", "", "test.mp4", str(root / "test.mp4"),
                         None, "created", "upload", "", "", ""))
                web_app.save_segments("test", [{"id": "s1", "start": 0, "end": 1,
                    "raw_text": "Hello", "edited_text": "Hello", "words": [],
                    "flags": [], "reviewed": False, "needs_realign": False}])
                web_app.process("test")
                self.assertEqual(web_app.get_job("test")["status"], "ready")
                finish.assert_called_once_with("test")

    def test_automatic_finish_prepares_translates_and_dubs(self):
        with patch.object(web_app, "get_job", side_effect=[
                {"status": "ready"}, {"translation_status": "done"}]), \
             patch.object(web_app, "prepare") as prepare, \
             patch.object(web_app, "translate_job") as translate, \
             patch.object(web_app, "dub_job") as dub:
            web_app.translate_then_dub("test")
        prepare.assert_called_once_with("test")
        translate.assert_called_once_with("test")
        dub.assert_called_once_with("test")

    def test_create_job_after_translation_schema_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            uploads.mkdir()
            with patch.object(web_app, "DB", root / "jobs.sqlite3"), patch.object(web_app, "UPLOADS", uploads):
                web_app.initialize()
                upload = UploadFile(filename="course.mp4", file=io.BytesIO(b"video data"))
                job = web_app.create_job(upload, "课程")
                self.assertEqual(job["status"], "created")
                self.assertEqual(job["translation_status"], "not_started")
                self.assertEqual(Path(job["upload_path"]).read_bytes(), b"video data")


if __name__ == "__main__":
    unittest.main()
