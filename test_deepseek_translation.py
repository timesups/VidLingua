"""Offline checks for DeepSeek request format, ID validation, and translation resume."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import deepseek_translation as deepseek
import web_app


class DeepSeekTranslationTests(unittest.TestCase):
    def test_configured_parallel_translation_preserves_group_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "deepseek.json"
            deepseek.save_config(config_path, {**deepseek.DEFAULTS, "concurrency": 2})
            packet = {"title": "Test", "groups": [{"id": f"g{i}", "segments": [
                {"id": f"s{i}", "edited_text": f"Line {i}", "start": i,
                 "end": i + 1, "duration_seconds": 1, "review_flags": []}]} for i in range(3)]}
            (root / "test-translation-input.json").write_text(json.dumps(packet), encoding="utf-8")
            lock = threading.Lock()
            active = peak = 0
            def translate(_key, _config, _packet, group):
                nonlocal active, peak
                self.assertTrue(group["segments"][0]["edited_text"].startswith("Corrected "))
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(.04 if group["id"] == "g0" else .01)
                with lock:
                    active -= 1
                source = group["segments"][0]
                return ([{"id": source["id"], "start": source["start"], "end": source["end"],
                          "en": source["edited_text"], "zh": "你好", "flags": []}], {"total_tokens": 1})
            def correct(_key, _config, _packet, group, _next):
                return ([{"id": segment["id"], "en": "Corrected " + segment["edited_text"], "uncertain": False}
                         for segment in group["segments"]], {})
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "DEEPSEEK_CONFIG", config_path), \
                 patch.object(web_app, "get_api_key", return_value="fake-key"), \
                 patch.object(web_app, "glossary_candidates", return_value=[]), \
                 patch.object(web_app, "refine_glossary", return_value=("测试课程", [], {})), \
                 patch.object(web_app, "correct_english_group", side_effect=correct), \
                 patch.object(web_app, "translate_group", side_effect=translate):
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "Test", "", "", "test.mp4", "", None, "prepared", "prepared", "", "", ""))
                web_app.translate_job("test")
                self.assertEqual(web_app.get_job("test")["translation_status"], "done")
            result = json.loads((root / "test-translation.json").read_text(encoding="utf-8"))
            self.assertEqual(peak, 2)
            self.assertEqual([group["id"] for group in result["groups"]], ["g0", "g1", "g2"])
            self.assertIn("Corrected Line 0", (root / "test-corrected-en.srt").read_text(encoding="utf-8-sig"))

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

    def test_english_correction_uses_context_and_preserves_ids(self):
        packet = {"title": "Geometry"}
        group = {"id": "g1", "previous_context": "We use a vertex shader.", "segments": [
            {"id": "s1", "edited_text": "The shaper has two inputs."}]}
        response = {"content": {"segments": [
            {"id": "s1", "en": "The shader has two inputs.", "uncertain": False}]}, "usage": {}}
        with patch.object(deepseek, "call_deepseek", return_value=response) as call:
            corrected, _ = deepseek.correct_english_group("key", deepseek.DEFAULTS, packet, group,
                                                           "Now compile the shader.")
        self.assertEqual(corrected[0]["en"], "The shader has two inputs.")
        self.assertIn("Now compile the shader.", call.call_args.args[2][1]["content"])
        self.assertIn("We use a vertex shader.", call.call_args.args[2][1]["content"])

    def test_pronunciation_annotations_are_validated(self):
        annotations = deepseek.clean_pronunciations("银行账户", [
            {"text": "行", "pinyin": "hang2", "alias": "航"},
            {"text": "银", "pinyin": "yin2", "alias": "银"},
            {"text": "不在句中", "pinyin": "bu4 zai4 ju4 zhong1", "alias": "这里"},
        ])
        self.assertEqual(annotations, [{"text": "行", "pinyin": "hang2", "alias": "航"}])

    def test_course_analysis_filters_unrelated_terms(self):
        packet = {"title": "Lecture", "groups": [{"segments": [
            {"edited_text": "Texture baking is useful for shader design."}]}]}
        candidates = [{"en": "shader", "count": 3, "example": "A shader."},
                      {"en": "texture baking", "count": 2, "example": "Texture baking is useful."}]
        response = {"content": {"subject": "图形学中的纹理烘焙与着色器", "terms": [
            {"en": "shader", "zh": "着色器"},
            {"en": "texture baking", "zh": "纹理烘焙"},
            {"en": "invented", "zh": "编造"}]}, "usage": {"total_tokens": 20}}
        with patch.object(deepseek, "call_deepseek", return_value=response):
            subject, terms, usage = deepseek.refine_glossary("fake-key", deepseek.DEFAULTS, packet, candidates)
        self.assertIn("图形学", subject)
        self.assertEqual([term["en"] for term in terms], ["shader", "texture baking"])
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
                return "光学课程", [{"en": "laser", "zh": "激光", "source": "deepseek"}], {"total_tokens": 5}

            def translate(_key, _config, _packet, group):
                calls.append(group["id"])
                self.assertIn("laser = 激光", _packet["glossary"])
                self.assertEqual(_packet["subject"], "光学课程")
                if group["id"] == "g002" and len(calls) == 2:
                    raise RuntimeError("temporary failure")
                source = group["segments"][0]
                return ([{"id": source["id"], "start": source["start"], "end": source["end"],
                          "en": source["edited_text"], "zh": "你好。", "flags": []}],
                        {"total_tokens": 10})

            def correct(_key, _config, _packet, group, _next):
                return ([{"id": segment["id"], "en": segment["edited_text"], "uncertain": False}
                         for segment in group["segments"]], {})

            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "get_api_key", return_value="fake-key"), \
                 patch.object(web_app, "glossary_candidates", return_value=[{"en":"laser","count":2,"example":"laser"}]), \
                 patch.object(web_app, "refine_glossary", side_effect=refine), \
                 patch.object(web_app, "correct_english_group", side_effect=correct), \
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
                self.assertIn("00:00:01,000 --> 00:00:02,000\n你好。\nHello",
                              bilingual.read_text(encoding="utf-8-sig"))
                bilingual.unlink()
                web_app.download_bilingual_srt("test")  # Existing translations can be exported too.
                self.assertEqual(bilingual.read_text(encoding="utf-8-sig").count("\n你好。\nHello"), 2)
                with patch.object(web_app.executor, "submit", return_value=None):
                    restarted = web_app.start_translation("test", force=True)
                self.assertEqual(restarted["translation_status"], "queued")
                self.assertTrue((root / "test-previous-zh.srt").is_file())
                self.assertTrue((root / "test-previous-bilingual.srt").is_file())
                self.assertTrue((root / "test-translation-input.json").is_file())


if __name__ == "__main__":
    unittest.main()
