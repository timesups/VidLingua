"""Regression checks for local job creation."""

import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import UploadFile

from vidlingua import web_app


class JobCreationTests(unittest.TestCase):
    def test_rerun_resets_segments_and_outputs_but_keeps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            upload = uploads / "test" / "source.mp4"
            upload.parent.mkdir(parents=True)
            upload.write_bytes(b"video")
            report = root / "test-translation.json"
            report.write_text("{}", encoding="utf-8")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", uploads), patch.object(web_app.executor, "submit") as submit:
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at,
                         translation_status,dubbing_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "Test", "", "", "source.mp4", str(upload), None, "prepared", "done", "", "", "",
                         "done", "done"))
                web_app.save_segments("test", [{"id": "s1", "start": 0, "end": 1, "raw_text": "Hello",
                    "edited_text": "Hello", "words": [], "flags": [], "reviewed": False, "needs_realign": False}])
                result = web_app.rerun_job("test")
                self.assertEqual(result["status"], "queued")
                self.assertEqual(result["translation_status"], "not_started")
                self.assertEqual(web_app.segment_rows("test"), [])
                self.assertFalse(report.exists())
                self.assertTrue(upload.exists())
                submit.assert_called_once_with(web_app.process, "test", True)

    def test_delete_removes_job_files_and_keeps_shared_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            run = root / "runs" / "shared"
            run.mkdir(parents=True)
            (run / "source.mp4").write_bytes(b"shared")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", uploads):
                web_app.initialize()
                with web_app.connect() as conn:
                    for job_id in ("first", "second"):
                        upload = uploads / job_id / "source.mp4"
                        upload.parent.mkdir(parents=True)
                        upload.write_bytes(b"video")
                        conn.execute("""INSERT INTO jobs
                            (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (job_id, job_id, "", "", "source.mp4", str(upload), str(run), "prepared", "done", "", "", ""))
                (root / "first-dubbed.mp4").write_bytes(b"output")
                cache = root / "dubbing" / "first"
                cache.mkdir(parents=True)
                (cache / "clip.wav").write_bytes(b"audio")
                self.assertEqual(web_app.delete_job("first"), {"deleted": "first"})
                with self.assertRaises(web_app.HTTPException):
                    web_app.get_job("first")
                self.assertEqual(web_app.get_job("second")["id"], "second")
                self.assertTrue((run / "source.mp4").exists())
                self.assertFalse((root / "first-dubbed.mp4").exists())
                self.assertFalse((uploads / "first").exists())
                self.assertFalse(cache.exists())

    def test_bulk_actions_reject_running_job_without_touching_others(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"):
                web_app.initialize()
                with web_app.connect() as conn:
                    for job_id, status in (("idle", "prepared"), ("busy", "running")):
                        conn.execute("""INSERT INTO jobs
                            (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (job_id, job_id, "", "", "source.mp4", "", None, status, "import", "", "", ""))
                with self.assertRaises(web_app.HTTPException) as error:
                    web_app.bulk_delete_jobs(web_app.JobSelection(ids=["idle", "busy"]))
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(web_app.get_job("idle")["id"], "idle")

    def test_bulk_rerun_queues_each_selected_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", uploads), patch.object(web_app.executor, "submit") as submit:
                web_app.initialize()
                with web_app.connect() as conn:
                    for job_id in ("first", "second"):
                        source = uploads / job_id / "source.mp4"
                        source.parent.mkdir(parents=True)
                        source.write_bytes(b"video")
                        conn.execute("""INSERT INTO jobs
                            (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (job_id, job_id, "", "", "source.mp4", str(source), None, "prepared", "done", "", "", ""))
                result = web_app.bulk_rerun_jobs(web_app.JobSelection(ids=["first", "second"]))
                self.assertEqual([job["status"] for job in result["jobs"]], ["queued", "queued"])
                self.assertEqual([call.args[1] for call in submit.call_args_list], ["first", "second"])

    def test_rerun_reuses_cached_youtube_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "cached.mp4"
            source.write_bytes(b"video")
            run = root / "run"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"media": {"duration_seconds": 2}}), encoding="utf-8")
            (run / "transcript_original").mkdir()
            (run / "transcript_original" / "asr_audio.json").write_text(
                json.dumps({"language": "en", "segments": []}), encoding="utf-8")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "download_youtube") as download, \
                 patch.object(web_app, "import_media", return_value=run), \
                 patch.object(web_app, "translate_then_dub"):
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at,
                         separate_vocals,source_url)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "Test", "", "", "cached.mp4", str(source), None, "queued", "import", "", "", "", 0,
                         "https://www.youtube.com/watch?v=abcdefghijk"))
                web_app.process("test", True)
                download.assert_not_called()
                self.assertEqual(web_app.get_job("test")["status"], "ready")

    def test_finished_video_preview_is_inline(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_app, "DATA", Path(directory)), \
             patch.object(web_app, "get_job", return_value={"dubbing_status": "done"}):
            path = Path(directory) / "job-dubbed.mp4"
            path.write_bytes(b"0123456789")
            response = web_app.preview_dubbed_video("job")
            self.assertEqual(response.path, path)
            self.assertEqual(response.media_type, "video/mp4")
            self.assertNotIn("attachment", response.headers.get("content-disposition", ""))

    def test_voice_clip_previews_cached_sentence(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(web_app, "DATA", Path(directory)), \
             patch.object(web_app, "get_job", return_value={"id": "job"}):
            root = Path(directory)
            (root / "job-dubbing-report.json").write_text(json.dumps({
                "region": "eastasia", "voice": "test-voice", "timings": [
                    {"id": "s1", "azure_rate": 1.0, "post_speed": 1.0}]}), encoding="utf-8")
            (root / "job-translation.json").write_text(json.dumps({
                "groups": [{"segments": [{"id": "s1", "zh": "你好"}]}]}), encoding="utf-8")
            cache = root / "dubbing" / "job"
            cache.mkdir(parents=True)
            key = web_app.clip_cache_key({"id": "s1", "zh": "你好"},
                                         {"region": "eastasia", "voice": "test-voice"}, 1.0)
            with wave.open(str(cache / f"s1-{key}.wav"), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(web_app.SAMPLE_RATE)
                wav.writeframes(b"\x00\x00" * 2400)
            response = web_app.voice_clip("job", "s1")
            self.assertEqual(response.media_type, "audio/wav")
            self.assertTrue(response.body.startswith(b"RIFF"))
            (root / "job-dubbing-report.json").write_text(json.dumps({
                "region": "eastasia", "voice": "test-voice", "stale_segments": ["s1"],
                "timings": [{"id": "s1", "azure_rate": 1.0, "post_speed": 1.0}]}), encoding="utf-8")
            (root / "job-translation.json").write_text(json.dumps({
                "groups": [{"segments": [{"id": "s1", "zh": "新台词"}]}]}), encoding="utf-8")
            self.assertTrue(web_app.voice_clip("job", "s1").body.startswith(b"RIFF"))
            with self.assertRaises(web_app.HTTPException):
                web_app.voice_clip("job", "unknown")

    def test_chinese_edit_keeps_review_report_and_marks_old_audio(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(web_app, "DATA", Path(directory)), \
             patch.object(web_app, "get_job", return_value={
                 "translation_status": "done", "dubbing_status": "needs_review"}), \
             patch.object(web_app, "update_job"):
            root = Path(directory)
            (root / "job-translation.json").write_text(json.dumps({"groups": [{"segments": [
                {"id": "s1", "zh": "旧台词", "en": "old", "start": 0, "end": 1},
                {"id": "s2", "zh": "旧句二", "en": "second", "start": 2, "end": 3}]}]}), encoding="utf-8")
            report = root / "job-dubbing-report.json"
            report.write_text(json.dumps({"issues": [{"id": "s1"}]}), encoding="utf-8")
            (root / "job-voice.wav").write_bytes(b"old")
            web_app.edit_chinese("job", "s1", web_app.ChineseEdit(zh="新台词"))
            web_app.edit_chinese("job", "s2", web_app.ChineseEdit(zh="新句二"))
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["stale_segments"], ["s1", "s2"])
            self.assertFalse((root / "job-voice.wav").exists())
            self.assertEqual(json.loads((root / "job-translation.json").read_text(encoding="utf-8"))
                             ["groups"][0]["segments"][0]["zh"], "新台词")

    def test_drop_false_segment_removes_it_from_translation_and_report(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(web_app, "DATA", Path(directory)), \
             patch.object(web_app, "get_job", return_value={
                 "translation_status": "done", "dubbing_status": "needs_review"}), \
             patch.object(web_app, "update_job"):
            root = Path(directory)
            (root / "job-translation.json").write_text(json.dumps({"groups": [
                {"id": "g1", "segments": [
                    {"id": "s1", "zh": "谢谢", "en": "Thanks.", "start": 1, "end": 1.2},
                    {"id": "s2", "zh": "下一句", "en": "Next.", "start": 1.4, "end": 2}]}]}), encoding="utf-8")
            report = root / "job-dubbing-report.json"
            report.write_text(json.dumps({"issues": [{"id": "s1", "kind": "timeline_limit"}, {"id": "s2"}],
                                          "stale_segments": ["s1"], "affected_segments": 2}), encoding="utf-8")
            (root / "job-voice.wav").write_bytes(b"old")
            web_app.drop_translation_segment("job", "s1")
            saved = json.loads((root / "job-translation.json").read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in saved["groups"][0]["segments"]], ["s2"])
            self.assertNotIn("Thanks.", (root / "job-bilingual.srt").read_text(encoding="utf-8-sig"))
            updated = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in updated["issues"]], ["s2"])
            self.assertEqual(updated["dropped_segments"][0]["en"], "Thanks.")
            self.assertEqual(updated["stale_segments"], [])
            self.assertFalse((root / "job-voice.wav").exists())

    def test_current_stage_progress_and_log_tail(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(web_app, "DATA", Path(directory)):
            log = Path(directory) / "test.log"
            log.write_text("[10:00:00] 阶段：separate\n 38%|####| 1754/4655 [22:40<43:03]\r"
                           " 39%|####| 1800/4655 [23:00<42:00]\n", encoding="utf-8")
            job = {"status": "running", "stage": "separate", "translation_status": "not_started",
                   "dubbing_status": "not_started"}
            progress = web_app.stage_progress(job, "test")
            self.assertEqual(progress["label"], "人声分离 1800/4655 块")
            self.assertGreater(progress["percent"], 38)
            with patch.object(web_app, "get_job", return_value=job):
                self.assertIn("1800/4655", web_app.log("test")["text"])
            log.write_text("[10:10:00] 阶段：transcribe\nProgress: 67.50%...\n", encoding="utf-8")
            job["stage"] = "transcribe"
            self.assertEqual(web_app.stage_progress(job, "test")["percent"], 67.5)
            log.write_text("[10:20:00] 阶段：download\nYouTube: 65%\n", encoding="utf-8")
            job["stage"] = "download"
            self.assertEqual(web_app.stage_progress(job, "test")["percent"], 65)

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
                job = web_app.create_job(upload, "课程", True)
                self.assertEqual(job["status"], "created")
                self.assertEqual(job["translation_status"], "not_started")
                self.assertEqual(job["separate_vocals"], 1)
                self.assertEqual(job["dubbing_engine"], "azure")
                self.assertEqual(Path(job["upload_path"]).read_bytes(), b"video data")
                no_split = web_app.create_job(
                    UploadFile(filename="course.mp4", file=io.BytesIO(b"another video")),
                    "不分离", False)
                self.assertEqual(no_split["separate_vocals"], 0)

    def test_create_youtube_job_waits_to_download_and_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "UPLOADS", root / "uploads"):
                web_app.initialize()
                job = web_app.create_youtube_job(web_app.YouTubeJobCreate(
                    url="https://youtu.be/abcdefghijk?t=4", separate_vocals=False))
                self.assertEqual(job["status"], "created")
                self.assertEqual(job["stage"], "download")
                self.assertEqual(job["source_url"], "https://www.youtube.com/watch?v=abcdefghijk")
                self.assertEqual(job["separate_vocals"], 0)
                self.assertEqual(job["dubbing_engine"], "azure")
                doubao = web_app.create_youtube_job(web_app.YouTubeJobCreate(
                    url="https://www.youtube.com/watch?v=abcdefghijk", dubbing_engine="doubao"))
                self.assertEqual(doubao["dubbing_engine"], "doubao")
                self.assertFalse(Path(job["upload_path"]).exists())

    def test_youtube_process_downloads_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "transcript_original").mkdir(parents=True)
            (run / "manifest.json").write_text(
                json.dumps({"media": {"duration_seconds": 2}}), encoding="utf-8")
            (run / "transcript_original" / "asr_audio.json").write_text("{}", encoding="utf-8")

            def fake_download(url, destination, log, config, cookies):
                self.assertEqual(url, "https://www.youtube.com/watch?v=abcdefghijk")
                self.assertEqual(web_app.get_job("test")["stage"], "download")
                self.assertEqual(config, web_app.YOUTUBE_CONFIG)
                self.assertEqual(cookies, web_app.YOUTUBE_COOKIES)
                destination.parent.mkdir(parents=True)
                destination.write_bytes(b"video")
                return {"id": "abcdefghijk", "title": "Downloaded lesson"}

            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "download_youtube", side_effect=fake_download), \
                 patch.object(web_app, "import_media", return_value=run) as imported, \
                 patch.object(web_app.subprocess, "run", return_value=Mock(returncode=0)), \
                 patch.object(web_app, "translate_then_dub"):
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,
                         created_at,updated_at,separate_vocals,source_url)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "YouTube abcdefghijk", "", "", "youtube-abcdefghijk.mp4",
                         str(root / "uploads" / "youtube-abcdefghijk.mp4"), None,
                         "created", "download", "", "", "", 0,
                         "https://www.youtube.com/watch?v=abcdefghijk"))
                web_app.save_segments("test", [{"id": "s1", "start": 0, "end": 1,
                    "raw_text": "Hello", "edited_text": "Hello", "words": [],
                    "flags": [], "reviewed": False, "needs_realign": False}])
                web_app.process("test")
                self.assertEqual(web_app.get_job("test")["status"], "ready")
                self.assertEqual(web_app.get_job("test")["title"], "Downloaded lesson")
                imported.assert_called_once_with(root / "uploads" / "youtube-abcdefghijk.mp4",
                                                 web_app.ROOT / "runs")

    def test_youtube_settings_hide_saved_cookie_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(web_app, "YOUTUBE_CONFIG", root / "youtube.json"), \
                 patch.object(web_app, "YOUTUBE_COOKIES", root / "cookies.txt"), \
                 patch.object(web_app, "test_youtube_connection", return_value={
                     "ok": True, "video_id": "jNQXAC9IVRw", "title": "Me at the zoo"}) as checker:
                result = web_app.configure_youtube(web_app.YouTubeConfigEdit(
                    proxy_mode="local", proxy_port="7890",
                    cookie_content="# Netscape HTTP Cookie File\n"
                                   ".youtube.com\tTRUE\t/\tTRUE\t0\tTEST\tsecret\n"))
                self.assertTrue(result["cookie_configured"])
                self.assertEqual(result["proxy_port"], "7890")
                self.assertNotIn("secret", json.dumps(result))
                self.assertEqual(web_app.test_youtube()["ok"], True)
                checker.assert_called_once_with(root / "youtube.json", root / "cookies.txt")
                cleared = web_app.configure_youtube(web_app.YouTubeConfigEdit(
                    proxy_mode="system", remove_cookies=True))
                self.assertFalse(cleared["cookie_configured"])

    def test_without_separation_transcribes_original_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "transcript_original").mkdir(parents=True)
            (run / "asr_audio.wav").write_bytes(b"original")
            (run / "manifest.json").write_text(
                json.dumps({"media": {"duration_seconds": 2}}), encoding="utf-8")
            (run / "transcript_original" / "asr_audio.json").write_text("{}", encoding="utf-8")
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "import_media", return_value=run), \
                 patch.object(web_app.subprocess, "run") as command, \
                 patch.object(web_app, "translate_then_dub") as finish:
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at,separate_vocals)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "Test", "", "", "test.mp4", str(root / "test.mp4"),
                         None, "created", "upload", "", "", "", 0))
                web_app.save_segments("test", [{"id": "s1", "start": 0, "end": 1,
                    "raw_text": "Hello", "edited_text": "Hello", "words": [],
                    "flags": [], "reviewed": False, "needs_realign": False}])
                web_app.process("test")
                command.assert_not_called()
                finish.assert_called_once_with("test")
                self.assertEqual(web_app.get_job("test")["status"], "ready")
                self.assertEqual(web_app.media("test", "vocals").path, run / "asr_audio.wav")
                with self.assertRaises(web_app.HTTPException):
                    web_app.media("test", "instrumental")


if __name__ == "__main__":
    unittest.main()
