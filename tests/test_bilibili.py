import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from vidlingua.providers import bilibili
from vidlingua import web_app


class BilibiliMetaTests(unittest.TestCase):
    def test_job_draft_keeps_selected_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            bilibili.save_config(config, {"tid": 229, "tags": ""})
            with patch.object(web_app, "DATA", root), patch.object(web_app, "BILIBILI_CONFIG", config):
                saved = web_app._save_bilibili_draft("video", web_app.BilibiliDraft(
                    title="标题", desc="简介", tags="", tid=85), require_text=True)
                self.assertEqual(saved["tid"], 85)
                self.assertEqual(saved["tags"], "")
                self.assertEqual(web_app._bilibili_view("video")["tid"], 85)

    def test_automatic_publish_uses_generated_draft(self):
        meta = {"status": "idle", "title": "中文标题", "desc": "中文简介", "tags": "翻译", "tid": 85}
        with patch.object(web_app, "_bilibili_view", return_value=meta), \
             patch.object(web_app, "generate_bilibili_meta", return_value=meta) as generate, \
             patch.object(web_app, "publish_bilibili") as publish:
            web_app._auto_publish_bilibili("video")
        generate.assert_called_once_with("video")
        self.assertEqual(publish.call_args.args[1].tid, 85)
        self.assertEqual(publish.call_args.args[1].title, "中文标题")

    def test_automatic_publish_stops_if_deepseek_metadata_fails(self):
        with patch.object(web_app, "_bilibili_view", return_value={"status": "idle"}), \
             patch.object(web_app, "generate_bilibili_meta", return_value={
                 "warning": "模型结果无法使用"}), \
             patch.object(web_app, "publish_bilibili") as publish:
            with self.assertRaisesRegex(RuntimeError, "模型结果无法使用"):
                web_app._auto_publish_bilibili("video")
        publish.assert_not_called()

    def test_completed_youtube_job_respects_publish_choice(self):
        job = {"source_url": "https://www.youtube.com/watch?v=abcdefghijk",
               "publish_to_bilibili": 1, "dubbing_status": "done", "batch_id": None}
        with patch.object(web_app.dubbing_service, "dub_job"), \
             patch.object(web_app, "get_job", return_value=job), \
             patch.object(web_app, "_auto_publish_bilibili") as publish:
            web_app.dub_job("video")
            publish.assert_called_once_with("video")
            job["publish_to_bilibili"] = 0
            web_app.dub_job("video")
            publish.assert_called_once()

    def test_completed_local_job_can_publish(self):
        job = {"source_url": "", "publish_to_bilibili": 1,
               "dubbing_status": "done", "batch_id": None}
        with patch.object(web_app.dubbing_service, "dub_job"), \
             patch.object(web_app, "get_job", return_value=job), \
             patch.object(web_app, "_auto_publish_bilibili") as publish:
            web_app.dub_job("local-video")
        publish.assert_called_once_with("local-video")

    def test_local_metadata_uses_subtitles_and_source_title(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "local-video-dubbed-zh.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n字幕里的中文内容\n", encoding="utf-8")
            job = {"translation_status": "done", "filename": "source.mp4",
                   "title": "源标题", "source_url": ""}
            generated = {"title": "中文标题", "desc": "简介", "tags": "翻译", "dynamic": "",
                         "warning": ""}
            with patch.object(web_app, "DATA", root), \
                 patch.object(web_app, "BILIBILI_CONFIG", root / "config.json"), \
                 patch.object(web_app, "get_job", return_value=job), \
                 patch.object(web_app, "get_api_key", return_value="key"), \
                 patch.object(web_app, "load_config", return_value={}), \
                 patch.object(bilibili, "generate_meta", return_value=generated) as generate:
                saved = web_app.generate_bilibili_meta("local-video")
            self.assertEqual(saved["status"], "ready")
            self.assertEqual(generate.call_args.kwargs["subtitle_text"], "字幕里的中文内容")
            self.assertEqual(generate.call_args.kwargs["original_title"], "源标题")
            self.assertEqual(generate.call_args.kwargs["source_url"], "")

    def test_cookie_parser_accepts_browser_string_and_json(self):
        parsed = bilibili.parse_cookie_string("SESSDATA=abc; bili_jct=xyz")
        self.assertEqual(parsed["SESSDATA"], "abc")
        self.assertEqual(parsed["bili_jct"], "xyz")
        wrapped = bilibili.parse_cookie_string(json.dumps({"cookies": {"SESSDATA": "a", "bili_jct": "b"}}))
        self.assertEqual(wrapped["bili_jct"], "b")

    def test_missing_cookie_does_not_call_bilibili(self):
        with tempfile.TemporaryDirectory() as directory:
            status = bilibili.login_status(Path(directory) / "cookies.json")
        self.assertFalse(status["logged_in"])

    def test_saving_cookie_does_not_wait_for_network_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            with patch.object(bilibili, "login_status") as verify:
                result = bilibili.save_cookie_login(path, "SESSDATA=abc; bili_jct=xyz")
            self.assertTrue(result["saved"])
            self.assertEqual(bilibili.load_cookies(path)["SESSDATA"], "abc")
            verify.assert_not_called()

    def test_truncated_cookie_is_rejected_without_exposing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            with self.assertRaisesRegex(ValueError, "bili_ticket") as error:
                bilibili.save_cookie_login(
                    path, "SESSDATA=abc; bili_jct=xyz; bili_ticket=short…")
            self.assertNotIn("short", str(error.exception))
            self.assertFalse(path.exists())
            with self.assertRaisesRegex(ValueError, "bili_ticket"):
                bilibili._session({"SESSDATA": "abc", "bili_ticket": "short…"})

    def test_preupload_forbidden_has_actionable_message(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"video")
            response = Mock(status_code=403, headers={"Content-Type": "application/json"})
            response.json.return_value = {"message": "权限不足"}
            session = Mock()
            session.get.return_value = response
            with self.assertRaisesRegex(RuntimeError, "HTTP 403.*权限不足"):
                bilibili._upload_file(session, video, lambda *_: None)

    def test_meta_keeps_bilibili_limits_and_source_link(self):
        meta = bilibili.normalize_meta(
            {"title": "标题" * 40, "desc": "简介", "tag": ["配音", "#翻译", "配音"], "dynamic": "动态" * 40},
            filename="lesson.mp4")
        self.assertEqual(len(meta["title"]), 80)
        self.assertEqual(meta["tags"], "配音,翻译")
        self.assertLessEqual(len(meta["dynamic"]), 100)
        linked = bilibili.append_source_link(meta["desc"], "https://www.youtube.com/watch?v=abc")
        self.assertIn("原视频：https://www.youtube.com/watch?v=abc", linked)
        self.assertEqual(bilibili.append_source_link("简介", "https://example.com/video"), "简介")

    def test_plain_subtitles_drop_timestamps(self):
        text = bilibili.plain_subtitles("1\n00:00:01,000 --> 00:00:02,000\n这是中文台词\n")
        self.assertEqual(text, "这是中文台词")

    def test_generate_uses_deepseek_and_appends_original_link(self):
        with patch.object(bilibili, "call_deepseek", return_value={"content": {
            "title": "中文标题", "desc": "这是简介。", "tag": ["配音", "教程"], "dynamic": "分享配音"
        }}) as call:
            meta = bilibili.generate_meta("key", {"model": "deepseek-flash"}, filename="lesson.mp4",
                                          subtitle_text="这是中文台词", original_title="Lesson",
                                          source_url="https://youtu.be/abc")
        self.assertEqual(meta["title"], "中文标题")
        self.assertIn("原视频：https://youtu.be/abc", meta["desc"])
        self.assertEqual(call.call_args.args[2][0]["role"], "system")
        self.assertFalse(meta["warning"])

    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bilibili_config.json"
            bilibili.save_config(path, {"tid": 85, "tags": "配音，翻译"})
            self.assertEqual(bilibili.load_config(path), {"tid": 85, "tags": "配音,翻译"})
            bilibili.save_config(path, {"tid": 85, "tags": ""})
            self.assertEqual(bilibili.load_config(path), {"tid": 85, "tags": ""})
            self.assertEqual(bilibili.empty_meta(bilibili.load_config(path))["tags"], "")
            with self.assertRaises(ValueError):
                bilibili.save_config(path, {"tid": 1, "tags": "配音"})

    def test_orphaned_upload_is_marked_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orphan-bilibili.json"
            bilibili.save_job_meta(path, {"status": "uploading", "progress": 24.8,
                                          "message": "上传中 17/73", "title": "标题"})
            meta = bilibili.load_job_meta(path, bilibili.DEFAULTS)
            self.assertEqual(meta["status"], "error")
            self.assertEqual(meta["message"], "投稿已中断")
            self.assertIn("重新投稿", meta["error"])
            self.assertEqual(meta["title"], "标题")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "error")


if __name__ == "__main__":
    unittest.main()
