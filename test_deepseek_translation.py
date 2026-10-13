"""Offline checks for DeepSeek request format, ID validation, and translation resume."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import deepseek_translation as deepseek
import web_app


class DeepSeekTranslationTests(unittest.TestCase):
    def test_invalid_key_is_rejected_before_network(self):
        with self.assertRaisesRegex(ValueError, "非 ASCII"):
            deepseek.validate_api_key("sk-test中文")
        with patch.object(deepseek.requests, "post") as post:
            with self.assertRaises(ValueError):
                deepseek.call_deepseek("sk-test中文", deepseek.DEFAULTS, [])
        post.assert_not_called()

    def test_request_and_group_ids(self):
        packet = {"title": "Test", "subject": "Physics", "glossary": ["laser = 激光"]}
        group = {"id": "g001", "previous_context": "", "segments": [
            {"id": "s0001", "edited_text": "A laser.", "duration_seconds": 2,
             "start": 1, "end": 3, "review_flags": []},
        ]}
        response = Mock(ok=True)
        response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"segments": [{"id": "s0001", "zh": "一道激光。", "uncertain": False}]})}}],
            "usage": {"total_tokens": 30}}
        with patch.object(deepseek.requests, "post", return_value=response) as post:
            items, usage = deepseek.translate_group("fake-key", deepseek.DEFAULTS, packet, group)
        self.assertEqual(items[0]["zh"], "一道激光。")
        self.assertEqual(usage["total_tokens"], 30)
        self.assertEqual(post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(post.call_args.kwargs["json"]["thinking"], {"type": "disabled"})
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer fake-key")

    def test_glossary_filters_unrelated_terms_and_user_overrides(self):
        packet = {"title": "Lecture", "subject": "Graphics", "glossary": ["shader = 着色器"]}
        candidates = [{"en": "shader", "count": 3, "example": "A shader."},
                      {"en": "texture baking", "count": 2, "example": "Texture baking is useful."}]
        response = {"content": {"terms": [
            {"en": "shader", "zh": "错误覆盖"},
            {"en": "texture baking", "zh": "纹理烘焙"},
            {"en": "invented", "zh": "编造"}]}, "usage": {"total_tokens": 20}}
        with patch.object(deepseek, "call_deepseek", return_value=response):
            terms, usage = deepseek.refine_glossary("fake-key", deepseek.DEFAULTS, packet, candidates)
        self.assertEqual(terms, [{"en": "texture baking", "zh": "纹理烘焙", "source": "deepseek"}])
        self.assertEqual(usage["total_tokens"], 20)

    def test_resume_and_srt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = {"title": "Test", "subject": "", "glossary": [], "groups": [
                {"id": f"g{i:03d}", "previous_context": "", "segments": [
                    {"id": f"s{i:04d}", "edited_text": "Hello", "start": i,
                     "end": i + 1, "duration_seconds": 1, "review_flags": []}]}
                for i in (1, 2)]}
            (root / "test-translation-input.json").write_text(json.dumps(packet), encoding="utf-8")
            calls = []
            glossary_calls = []

            def refine(_key, _config, _packet, _candidates):
                glossary_calls.append("prepass")
                return [{"en": "laser", "zh": "激光", "source": "deepseek"}], {"total_tokens": 5}

            def translate(_key, _config, _packet, group):
                calls.append(group["id"])
                self.assertIn("laser = 激光", _packet["glossary"])
                if group["id"] == "g002" and len(calls) == 2:
                    raise RuntimeError("temporary failure")
                source = group["segments"][0]
                return ([{"id": source["id"], "start": source["start"], "end": source["end"],
                          "en": source["edited_text"], "zh": "你好。", "flags": []}],
                        {"total_tokens": 10})

            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "get_api_key", return_value="fake-key"), \
                 patch.object(web_app, "glossary_candidates", return_value=[{"en":"laser","count":2,"example":"laser"}]), \
                 patch.object(web_app, "refine_glossary", side_effect=refine), \
                 patch.object(web_app, "translate_group", side_effect=translate):
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "Test", "", "", "test.mp4", "", None, "prepared", "prepared", "", "", ""))
                web_app.translate_job("test")
                self.assertEqual(web_app.get_job("test")["translation_status"], "failed")
                self.assertEqual(len(json.loads((root / "test-translation.json").read_text(encoding="utf-8"))["groups"]), 1)
                web_app.translate_job("test")
                self.assertEqual(web_app.get_job("test")["translation_status"], "done")
                self.assertEqual(calls, ["g001", "g002", "g002"])
                self.assertEqual(glossary_calls, ["prepass"])
                self.assertEqual(json.loads((root / "test-glossary.json").read_text(encoding="utf-8"))["merged_terms"], ["laser = 激光"])
                self.assertIn("你好。", (root / "test-zh.srt").read_text(encoding="utf-8-sig"))
                bilingual = root / "test-bilingual.srt"
                self.assertIn("00:00:01,000 --> 00:00:02,000\nHello\n你好。",
                              bilingual.read_text(encoding="utf-8-sig"))
                bilingual.unlink()
                web_app.download_bilingual_srt("test")  # Existing translations can be exported too.
                self.assertEqual(bilingual.read_text(encoding="utf-8-sig").count("\nHello\n你好。"), 2)
                with patch.object(web_app.executor, "submit", return_value=None):
                    restarted = web_app.start_translation("test", force=True)
                self.assertEqual(restarted["translation_status"], "queued")
                self.assertTrue((root / "test-previous-zh.srt").is_file())
                self.assertTrue((root / "test-previous-bilingual.srt").is_file())
                self.assertTrue((root / "test-translation-input.json").is_file())


if __name__ == "__main__":
    unittest.main()
