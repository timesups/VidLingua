import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vidlingua.providers import bilibili


class BilibiliMetaTests(unittest.TestCase):
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
            with self.assertRaises(ValueError):
                bilibili.save_config(path, {"tid": 1, "tags": "配音"})


if __name__ == "__main__":
    unittest.main()
