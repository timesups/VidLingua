"""Offline checks for Doubao speech settings and synthesis parsing."""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from vidlingua.providers import azure_speech as azure
from vidlingua.providers import doubao_speech as doubao
from vidlingua import web_app


class DoubaoSpeechTests(unittest.TestCase):
    def test_speech_rate_maps_normal_speed_to_zero(self):
        self.assertEqual(doubao.speech_rate(1), 0)
        self.assertEqual(doubao.speech_rate(2), 100)
        self.assertEqual(doubao.speech_rate(0.5), -50)

    def test_parse_chunks_joins_pcm_and_stops_on_success(self):
        pcm = np.array([1000, -1000, 50], dtype="<i2").tobytes()
        audio = doubao.parse_chunks([
            json.dumps({"code": 0, "data": base64.b64encode(pcm).decode()}),
            "data: " + json.dumps({"code": 20000000, "message": "ok", "data": None}),
        ])
        self.assertEqual(audio, pcm)

    def test_parse_chunks_reports_provider_errors(self):
        with self.assertRaises(RuntimeError):
            doubao.parse_chunks([json.dumps({"code": 45000000, "message": "speaker permission denied"})])

    def test_synthesize_requests_pcm_and_returns_wav(self):
        pcm = np.array([1000, -1000], dtype="<i2").tobytes()
        response = Mock(ok=True, status_code=200)
        response.iter_lines.return_value = [
            json.dumps({"code": 0, "data": base64.b64encode(pcm).decode()}),
            json.dumps({"code": 20000000, "message": "ok", "data": None}),
        ]
        with patch.object(doubao.requests, "post", return_value=response) as post:
            wav = doubao.synthesize("12345678abcd", doubao.DEFAULTS, "你好", 1)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["req_params"]["audio_params"]["format"], "pcm")
        self.assertEqual(sent["req_params"]["audio_params"]["sample_rate"], 24000)
        self.assertEqual(sent["req_params"]["speaker"], doubao.DEFAULTS["speaker"])
        self.assertEqual(post.call_args.kwargs["headers"]["X-Api-Resource-Id"], "seed-tts-2.0")
        self.assertEqual(post.call_args.kwargs["headers"]["X-Api-Key"], "12345678abcd")
        self.assertEqual(azure.read_wav(wav).tolist(), [1000, -1000])

    def test_settings_do_not_echo_the_api_key(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_app, "DOUBAO_CONFIG", Path(directory) / "doubao.json"), \
             patch.object(web_app, "get_doubao_key", return_value="12345678abcd"), \
             patch.object(web_app, "set_doubao_key") as save_key:
            edit = web_app.DoubaoConfigEdit(api_key="12345678abcd", speaker="zh_female_vv_uranus_bigtts",
                                            resource_id="seed-tts-2.0", concurrency=2)
            response = web_app.configure_doubao(edit)
        save_key.assert_called_once_with("12345678abcd")
        self.assertEqual(response["concurrency"], 2)
        self.assertTrue(response["api_key_configured"])
        self.assertNotIn("12345678abcd", json.dumps(response))

    def test_job_can_choose_doubao_without_calling_azure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"media": {"duration_seconds": 3}}), encoding="utf-8")
            (root / "test-translation.json").write_text(json.dumps({"groups": [{"segments": [
                {"id": "s1", "start": .2, "end": 1.2, "en": "Hello", "zh": "你好", "flags": []}]}]}), encoding="utf-8")
            timings = [{"id": "s1", "audio_seconds": .5, "azure_rate": 1.0, "post_speed": 1.0}]
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "AZURE_CONFIG", root / "azure_config.json"), \
                 patch.object(web_app, "DOUBAO_CONFIG", root / "doubao_config.json"), \
                 patch.object(web_app, "get_azure_keys", side_effect=AssertionError("should not read Azure keys")), \
                 patch.object(web_app, "get_doubao_key", return_value="12345678abcd"), \
                 patch.object(web_app, "make_voice_track", return_value=(np.zeros(3 * azure.SAMPLE_RATE, dtype="<i2"), [], timings)) as synth, \
                 patch.object(web_app, "assemble_elastic_voice_track", return_value=np.zeros(3 * azure.SAMPLE_RATE, dtype="<i2")), \
                 patch.object(web_app, "render_video"):
                doubao.save_config(web_app.DOUBAO_CONFIG, doubao.DEFAULTS)
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at,translation_status,dubbing_engine)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "Test", "", "", "test.mp4", "", str(run), "prepared", "prepared", "", "", "", "done", "doubao"))
                web_app.dub_job("test")
                self.assertIs(synth.call_args.kwargs["synthesize_fn"], web_app.doubao_synthesize)
                self.assertEqual(synth.call_args.args[1]["engine"], "doubao")
                self.assertEqual(synth.call_args.args[1]["voice"], doubao.DEFAULTS["speaker"])
                report = json.loads((root / "test-dubbing-report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["engine"], "doubao")


if __name__ == "__main__":
    unittest.main()
