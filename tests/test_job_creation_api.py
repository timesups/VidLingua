"""Contract checks for the JSON job creation endpoint."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vidlingua import web_app


class JobCreationApiTests(unittest.TestCase):
    def test_route_is_registered(self):
        routes = {(route.path, tuple(getattr(route, "methods", ()))) for route in web_app.app.routes}
        self.assertTrue(any(path == "/api/jobs/create" and "POST" in methods for path, methods in routes))

    def test_local_path_queues_job_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lesson.mkv"
            source.write_bytes(b"sample video")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit") as submit:
                web_app.initialize()
                job = web_app.create_job_by_api(web_app.ApiJobCreate(
                    source="local", path=str(source), title="第一讲",
                    separate_vocals=False, dubbing_engine="azure"))
                self.assertEqual(job["status"], "queued")
                self.assertEqual(job["title"], "第一讲")
                self.assertEqual(job["batch_source_path"], str(source))
                self.assertFalse(job["separate_vocals"])
                self.assertFalse(job["publish_to_bilibili"])
                self.assertEqual(job["detail_url"], f"/#job/{job['id']}")
                self.assertTrue(source.exists())
                submit.assert_called_once_with(web_app.process, job["id"])

    def test_local_path_can_enable_automatic_bilibili_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lesson.mp4"
            source.write_bytes(b"video")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit"):
                web_app.initialize()
                job = web_app.create_job_by_api(web_app.ApiJobCreate(
                    source="local", path=str(source), publish_to_bilibili=True))
                self.assertTrue(job["publish_to_bilibili"])

    def test_youtube_duplicate_returns_existing_job_without_requeue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit") as submit:
                web_app.initialize()
                body = web_app.ApiJobCreate(source="youtube", url="https://www.youtube.com/watch?v=abcdefghijk")
                first = web_app.create_job_by_api(body)
                second = web_app.create_job_by_api(body)
                self.assertEqual(first["id"], second["id"])
                self.assertFalse(first["already_exists"])
                self.assertTrue(second["already_exists"])
                self.assertTrue(first["publish_to_bilibili"])
                submit.assert_called_once()

    def test_youtube_publish_choice_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit"):
                web_app.initialize()
                job = web_app.create_youtube_job(web_app.YouTubeJobCreate(
                    url="https://www.youtube.com/watch?v=abcdefghijk", publish_to_bilibili=False))
                self.assertFalse(job["publish_to_bilibili"])
                self.assertFalse(web_app.get_job(job["id"])["publish_to_bilibili"])

    def test_batch_path_queues_current_folder_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            videos = root / "videos"
            videos.mkdir()
            (videos / "one.mp4").write_bytes(b"video")
            nested = videos / "nested"
            nested.mkdir()
            (nested / "two.mp4").write_bytes(b"video")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit") as submit:
                web_app.initialize()
                batch = web_app.create_job_by_api(web_app.ApiJobCreate(source="batch", path=str(videos)))
                self.assertEqual(batch["kind"], "batch")
                self.assertEqual(batch["count"], 1)
                self.assertEqual(batch["detail_url"], f"/#batch/{batch['id']}")
                self.assertEqual(batch["output_dir"], str(videos / "Translate"))
                submit.assert_called_once()

    def test_missing_local_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit") as submit:
                web_app.initialize()
                with self.assertRaises(web_app.HTTPException) as error:
                    web_app.create_job_by_api(web_app.ApiJobCreate(source="local", path=str(root / "missing.mp4")))
                self.assertEqual(error.exception.status_code, 400)
                submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
