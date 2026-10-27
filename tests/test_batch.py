"""Batch job creation and local output checks."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from vidlingua import web_app
from vidlingua.pipeline.batch import prepare_video, scan_directory


class BatchTests(unittest.TestCase):
    def test_native_folder_picker_fills_local_path_or_can_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Mock(returncode=0, stdout=json.dumps(directory), stderr="")
            with patch.object(web_app.subprocess, "run", return_value=result) as runner:
                self.assertEqual(web_app.select_batch_folder(), {"path": directory})
                self.assertIn("vidlingua.api.folder_picker", runner.call_args.args[0])
            with patch.object(web_app.subprocess, "run", return_value=Mock(
                returncode=0, stdout="null\n", stderr="")):
                self.assertEqual(web_app.select_batch_folder(), {"path": None})

    def test_light_work_runs_while_heavy_media_stage_waits(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(web_app, "DATA", Path(directory)), \
             patch.object(web_app, "check_pause"):
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()
            light_completed = threading.Event()

            def first_heavy():
                with web_app.heavy_media_stage("first", "测试编码"):
                    first_entered.set()
                    release_first.wait(3)

            def second_heavy():
                with web_app.heavy_media_stage("second", "测试识别"):
                    second_entered.set()

            first = web_app.executor.submit(first_heavy)
            self.assertTrue(first_entered.wait(2))
            second = web_app.executor.submit(second_heavy)
            light = web_app.executor.submit(light_completed.set)
            try:
                self.assertTrue(light_completed.wait(2))
                self.assertFalse(second_entered.is_set())
            finally:
                release_first.set()
            first.result(timeout=3)
            second.result(timeout=3)
            light.result(timeout=3)
            self.assertTrue(second_entered.is_set())

    def test_scan_is_nonrecursive_and_includes_common_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.MOV").write_bytes(b"video")
            (root / "a.mp4").write_bytes(b"video")
            (root / "notes.txt").write_text("skip")
            (root / "nested").mkdir()
            (root / "nested" / "hidden.mp4").write_bytes(b"video")
            self.assertEqual([path.name for path in scan_directory(root)], ["a.mp4", "b.MOV"])

    def test_batch_lists_parent_and_keeps_child_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "videos"
            source.mkdir()
            (source / "first.mp4").write_bytes(b"video")
            (source / "second.mkv").write_bytes(b"video")
            (source / "nested").mkdir()
            (source / "nested" / "skip.mp4").write_bytes(b"video")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit") as submit:
                web_app.initialize()
                batch = web_app.create_batch(web_app.BatchCreate(path=str(source)))
                self.assertEqual(batch["count"], 2)
                self.assertEqual(len(batch["children"]), 2)
                self.assertEqual([item["kind"] for item in web_app.list_jobs()], ["batch"])
                self.assertEqual(submit.call_count, 2)
                first = web_app.job_detail(batch["children"][0]["id"])
                self.assertEqual(first["batch_id"], batch["id"])
                self.assertEqual(first["separate_vocals"], 1)
                self.assertTrue(first["batch_output_path"].startswith(str(source / "Translate")))

    def test_mp4_source_is_copied_and_finished_video_is_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "course.mp4"
            source.write_bytes(b"original")
            target = root / "uploads" / "course.mp4"
            prepare_video(source, target, root / "job.log")
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(source.read_bytes(), b"original")

            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit"):
                web_app.initialize()
                batch = web_app.create_batch(web_app.BatchCreate(path=str(root)))
                job_id = batch["children"][0]["id"]
                (root / f"{job_id}-dubbed.mp4").write_bytes(b"finished")

                def finish(_context, _job_id):
                    web_app.update_job(_job_id, dubbing_status="done")

                with patch.object(web_app.dubbing_service, "dub_job", side_effect=finish):
                    web_app.dub_job(job_id)
                self.assertEqual((root / "Translate" / "course.mp4.translated.mp4").read_bytes(),
                                 b"finished")

    def test_deleting_batch_removes_jobs_but_keeps_published_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lesson.mp4"
            source.write_bytes(b"video")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit"):
                web_app.initialize()
                batch = web_app.create_batch(web_app.BatchCreate(path=str(root)))
                child_id = batch["children"][0]["id"]
                web_app.update_job(child_id, status="ready", dubbing_status="done")
                output = root / "Translate" / "lesson.mp4.translated.mp4"
                output.parent.mkdir()
                output.write_bytes(b"finished")
                web_app.delete_batch(batch["id"])
                self.assertTrue(output.exists())
                with self.assertRaises(HTTPException):
                    web_app.get_batch(batch["id"])

    def test_home_bulk_actions_include_batch_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lesson.mp4").write_bytes(b"video")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit") as submit:
                web_app.initialize()
                batch = web_app.create_batch(web_app.BatchCreate(path=str(root)))
                child_id = batch["children"][0]["id"]
                web_app.update_job(child_id, status="ready")
                rerun = web_app.bulk_rerun_jobs(web_app.JobSelection(ids=[batch["id"]]))
                self.assertEqual([job["id"] for job in rerun["jobs"]], [child_id])
                self.assertEqual(submit.call_count, 2)
                with self.assertRaises(HTTPException):
                    web_app.bulk_delete_jobs(web_app.JobSelection(ids=[batch["id"]]))
                self.assertEqual(web_app.get_batch(batch["id"])["count"], 1)
                web_app.update_job(child_id, status="ready")
                self.assertEqual(web_app.bulk_delete_jobs(web_app.JobSelection(ids=[batch["id"]])),
                                 {"deleted": [batch["id"]]})
                with self.assertRaises(HTTPException):
                    web_app.get_batch(batch["id"])

    def test_same_named_subtitle_skips_whisperx_for_batch_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lesson.mp4"
            source.write_bytes(b"video")
            (root / "lesson.srt").write_text(
                "1\n00:00:01,000 --> 00:00:03,000\nAn English sentence.\n", encoding="utf-8")
            run = root / "run"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"media": {"duration_seconds": 5}}), encoding="utf-8")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"), \
                 patch.object(web_app.executor, "submit"), \
                 patch.object(web_app, "import_media", return_value=run), \
                 patch.object(web_app.subprocess, "run") as runner, \
                 patch.object(web_app, "translate_then_dub") as finish:
                web_app.initialize()
                batch = web_app.create_batch(web_app.BatchCreate(path=str(root), separate_vocals=False))
                child_id = batch["children"][0]["id"]
                web_app.process(child_id)
                runner.assert_not_called()
                self.assertEqual(web_app.get_job(child_id)["status"], "ready")
                self.assertEqual(web_app.segment_rows(child_id)[0]["edited_text"], "An English sentence.")
                finish.assert_called_once_with(child_id)


if __name__ == "__main__":
    unittest.main()
