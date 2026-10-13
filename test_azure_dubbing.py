"""Offline checks for bounded Azure synthesis and dubbing output."""

import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import azure_dubbing as azure
import web_app


def sample_wav(seconds: float) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(azure.SAMPLE_RATE)
        wav.writeframes(np.zeros(round(seconds * azure.SAMPLE_RATE), dtype="<i2").tobytes())
    return out.getvalue()


class AzureDubbingTests(unittest.TestCase):
    def test_azure_settings_accept_key_pool_without_echoing_keys(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_app, "AZURE_CONFIG", Path(directory) / "azure.json"), \
             patch.object(web_app, "get_azure_keys", return_value=["key-a", "key-b"]), \
             patch.object(web_app, "set_azure_keys") as save_keys:
            edit = web_app.AzureConfigEdit(api_keys="key-a\nkey-b", per_key_concurrency=3)
            response = web_app.configure_azure(edit)
        save_keys.assert_called_once_with(["key-a", "key-b"])
        self.assertEqual(response["key_count"], 2)
        self.assertEqual(response["per_key_concurrency"], 3)
        self.assertNotIn("key-a", str(response))

    def test_multiple_keys_obey_each_keys_concurrency_limit(self):
        config = {**azure.DEFAULTS, "voice": "zh-CN-Xiaochen:DragonHDLatestNeural",
                  "per_key_concurrency": 2}
        segments = [{"id": f"s{i}", "start": i * 2, "end": i * 2 + 1,
                     "zh": f"句子{i}"} for i in range(8)]
        lock = threading.Lock()
        active = {"key-a": 0, "key-b": 0}
        peaks = {"key-a": 0, "key-b": 0}
        def fake_synthesize(key, _config, _text, _rate):
            with lock:
                active[key] += 1
                peaks[key] = max(peaks[key], active[key])
            time.sleep(.03)
            with lock:
                active[key] -= 1
            return sample_wav(.4)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(azure, "synthesize", side_effect=fake_synthesize):
            azure.make_voice_track(segments, config, ["key-a", "key-b"],
                                   Path(directory), 17)
        self.assertEqual(peaks, {"key-a": 2, "key-b": 2})

    def test_hd_voice_uses_supported_ssml_and_fixed_azure_rate(self):
        voice = "zh-CN-Xiaochen:DragonHDLatestNeural"
        config = {**azure.DEFAULTS, "voice": voice}
        azure.validate_config(config)
        markup = azure.ssml("甲&乙", voice, 1.1)
        self.assertIn("甲&amp;乙", markup)
        self.assertNotIn("prosody", markup)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(azure, "synthesize", return_value=sample_wav(.5)) as synth:
            _, _, timings = azure.make_voice_track(
                [{"id": "s1", "start": 0, "end": 1, "zh": "你好"}], config,
                "fake-key", Path(directory), 2)
        self.assertEqual(timings[0]["azure_rate"], 1.0)
        self.assertEqual(synth.call_args.args[3], 1.0)

    def test_region_voice_catalogue_filters_chinese_and_includes_hd(self):
        voices = [
            {"Locale": "zh-CN", "ShortName": "zh-CN-XiaoxiaoNeural", "LocalName": "晓晓", "Gender": "Female"},
            {"Locale": "zh-CN", "ShortName": "zh-CN-Xiaochen:DragonHDLatestNeural", "LocalName": "晓辰 HD", "Gender": "Female"},
            {"Locale": "en-US", "ShortName": "en-US-AvaNeural", "LocalName": "Ava", "Gender": "Female"},
        ]
        result = azure.chinese_voices(voices)
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0]["hd"])
        with patch.object(web_app, "get_azure_key", return_value="fake-key"), \
                 patch.object(web_app, "get_azure_keys", return_value=["fake-key"]), \
             patch.object(web_app, "load_azure_config", return_value={**azure.DEFAULTS}), \
             patch.object(web_app, "azure_voice_list", return_value=voices):
            response = web_app.test_azure()
        self.assertEqual(response["region"], "eastasia")
        self.assertEqual(response["voice_count"], 2)

    def test_request_escapes_text_and_keeps_key_out_of_body(self):
        response = Mock(ok=True, content=sample_wav(1))
        config = {**azure.DEFAULTS, "region": "eastasia"}
        with patch.object(azure.requests, "post", return_value=response) as post:
            output = azure.synthesize("fake-key", config, "甲&乙 <测试>", 1.03)
        self.assertEqual(output, response.content)
        request = post.call_args
        self.assertIn("甲&amp;乙 &lt;测试&gt;", request.kwargs["data"].decode("utf-8"))
        self.assertNotIn("fake-key", request.kwargs["data"].decode("utf-8"))
        self.assertEqual(request.kwargs["headers"]["X-Microsoft-OutputFormat"], "riff-24khz-16bit-mono-pcm")

    def test_bounded_post_speed_and_overrun_report(self):
        config = {**azure.DEFAULTS, "region": "eastasia"}
        segments = [{"id": "s1", "start": 0.5, "end": 1.5, "zh": "这是一句比较长的中文台词。"},
                    {"id": "s2", "start": 2.0, "end": 3.0, "zh": "你好。"}]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(azure, "synthesize", side_effect=[sample_wav(2), sample_wav(.4)]):
            track, issues, timings = azure.make_voice_track(segments, config, "fake-key", Path(directory), 4)
        self.assertEqual(len(track), 5 * azure.SAMPLE_RATE)
        self.assertTrue(any(item["id"] == "s1" and item["kind"] == "overrun" for item in issues))
        self.assertGreaterEqual(timings[0]["post_speed"], 1.05)
        self.assertLessEqual(timings[0]["post_speed"], 1.134)
        self.assertLessEqual(timings[0]["azure_rate"] * timings[0]["post_speed"], 1.135)

    def test_post_adjust_maximum_is_half_and_default_stays(self):
        self.assertEqual(azure.DEFAULTS["max_post_adjust"], 0.15)
        azure.validate_config({**azure.DEFAULTS, "max_post_adjust": 0.5})
        with self.assertRaises(ValueError):
            azure.validate_config({**azure.DEFAULTS, "max_post_adjust": 0.51})
        config = {**azure.DEFAULTS, "region": "eastasia", "voice": "zh-CN-Xiaochen:DragonHDLatestNeural",
                  "max_post_adjust": 0.5}
        segments = [{"id": "s1", "start": 0.5, "end": 1.5, "zh": "这是一句比较长的中文台词。"}]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(azure, "synthesize", return_value=sample_wav(2)):
            _, _, timings = azure.make_voice_track(segments, config, "fake-key", Path(directory), 4)
        self.assertGreater(timings[0]["post_speed"], 1.12)
        self.assertLessEqual(timings[0]["post_speed"], 1.5)

    def test_global_baseline_uses_typical_sentences_not_one_long_outlier(self):
        config = {**azure.DEFAULTS, "max_post_adjust": .2}
        baseline = azure.choose_global_baseline([1.1, 1.12, 1.08, 3.0], [1, 1, 1, 1], config)
        self.assertEqual(baseline["measured_median_ratio"], 1.11)
        self.assertEqual(baseline["speed"], 1.08)

    def test_standard_voice_pilot_sets_one_azure_rate_before_local_fit(self):
        config = {**azure.DEFAULTS, "max_post_adjust": .2}
        segments = [{"id": f"s{i}", "start": 2.0 * i, "end": 2.0 * i + 1.0,
                     "zh": "这是一句课程解说"} for i in range(8)]
        baseline = {}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(azure, "synthesize", side_effect=lambda key, cfg, text, rate: sample_wav(1.5 / rate)) as synth:
            _, issues, timings = azure.make_voice_track(
                segments, config, "fake-key", Path(directory), 17, baseline_info=baseline)
        self.assertEqual(baseline["azure_rate"], 1.08)
        self.assertEqual(baseline["pilot"]["measured_segments"], 4)
        self.assertTrue(all(item["azure_rate"] == 1.08 for item in timings))
        self.assertTrue(all(item["post_speed"] <= baseline["speed"] * 1.05 + .001 for item in timings))
        self.assertGreater(synth.call_count, len(segments))
        self.assertEqual(issues, [])

    def test_hd_uses_one_measured_post_baseline_with_bounded_local_adjust(self):
        config = {**azure.DEFAULTS, "voice": "zh-CN-Xiaochen:DragonHDLatestNeural",
                  "max_post_adjust": .2}
        segments = [{"id": f"s{i}", "start": 2.0 * i, "end": 2.0 * i + 1.0,
                     "zh": "课程解说"} for i in range(8)]
        durations = [1.45] * 7 + [2.5]
        baseline = {}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(azure, "synthesize", side_effect=[sample_wav(d) for d in durations]):
            _, issues, timings = azure.make_voice_track(
                segments, config, "fake-key", Path(directory), 17, baseline_info=baseline)
        self.assertEqual(baseline["azure_rate"], 1.0)
        self.assertGreater(baseline["speed"], 1.0)
        self.assertTrue(all(item["post_speed"] >= baseline["speed"] for item in timings))
        self.assertTrue(all(item["post_speed"] <= baseline["speed"] * 1.05 + .001 for item in timings))
        self.assertTrue(any(item["kind"] == "overrun" for item in issues))

    def test_full_dubbing_job_with_cached_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"media": {"duration_seconds": 2}}), encoding="utf-8")
            (root / "test-translation.json").write_text(json.dumps({"groups": [{"segments": [
                {"id": "s1", "start": .2, "end": 1.2, "en": "Hello", "zh": "你好", "flags": []}]}]}), encoding="utf-8")
            config = {**azure.DEFAULTS, "region": "eastasia"}
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "AZURE_CONFIG", root / "azure_config.json"), \
                 patch.object(web_app, "get_azure_key", return_value="fake-key"), \
                 patch.object(web_app, "get_azure_keys", return_value=["fake-key"]), \
                 patch.object(web_app, "make_voice_track", return_value=(np.zeros(3 * azure.SAMPLE_RATE, dtype="<i2"), [], [
                     {"id": "s1", "audio_seconds": 1.0, "azure_rate": 1.0, "post_speed": 1.0}])), \
                 patch.object(web_app, "assemble_elastic_voice_track", return_value=np.zeros(3 * azure.SAMPLE_RATE, dtype="<i2")), \
                 patch.object(web_app, "render_video") as render:
                azure.save_config(web_app.AZURE_CONFIG, config)
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at,translation_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "Test", "", "", "test.mp4", "", str(run), "prepared", "prepared", "", "", "", "done"))
                web_app.dub_job("test")
                self.assertEqual(web_app.get_job("test")["dubbing_status"], "done")
                self.assertTrue((root / "test-voice.wav").is_file())
                self.assertIn("你好", (root / "test-dubbed-zh.srt").read_text(encoding="utf-8-sig"))
                render.assert_called_once()
                self.assertIsNone(render.call_args.args[6] if len(render.call_args.args) > 6 else render.call_args.kwargs.get("pieces"))

    def test_dub_job_slows_video_when_overrun_fits(self):
        timings = [{"id": "s1", "audio_seconds": 1.95, "azure_rate": 1.0,
                    "post_speed": 1.0}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"media": {"duration_seconds": 2}}), encoding="utf-8")
            (root / "test-translation.json").write_text(json.dumps({"groups": [{"segments": [
                {"id": "s1", "start": .2, "end": 1.2, "en": "Hello", "zh": "你好", "flags": []}]}]}), encoding="utf-8")
            config = {**azure.DEFAULTS, "region": "eastasia"}
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"), \
                 patch.object(web_app, "AZURE_CONFIG", root / "azure_config.json"), \
                 patch.object(web_app, "get_azure_key", return_value="fake-key"), \
                 patch.object(web_app, "get_azure_keys", return_value=["fake-key"]), \
                 patch.object(web_app, "make_voice_track", return_value=(np.zeros(3 * azure.SAMPLE_RATE, dtype="<i2"), [], timings)), \
                 patch.object(web_app, "assemble_elastic_voice_track", return_value=np.zeros(4 * azure.SAMPLE_RATE, dtype="<i2")), \
                 patch.object(web_app, "render_video") as render:
                azure.save_config(web_app.AZURE_CONFIG, config)
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at,translation_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "Test", "", "", "test.mp4", "", str(run), "prepared", "prepared", "", "", "", "done"))
                web_app.dub_job("test")
                self.assertEqual(web_app.get_job("test")["dubbing_status"], "done")
                self.assertIn("00:00:02,150", (root / "test-dubbed-zh.srt").read_text(encoding="utf-8-sig"))
                pieces = render.call_args.args[6]
                self.assertTrue(any(piece["speed"] < 1 for piece in pieces))
                report = json.loads((root / "test-dubbing-report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["issues"], [])
                self.assertEqual(report["retiming"][0]["id"], "s1")

    def test_ffmpeg_renders_video_with_background_and_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, bg, voice, output = (root / name for name in ("source.mp4", "bg.wav", "voice.wav", "dubbed.mp4"))
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "color=c=black:s=64x64:r=24:d=2", "-c:v", "libx264", str(source)], check=True)
            azure.write_wav(bg, np.zeros(2 * azure.SAMPLE_RATE, dtype="<i2"))
            azure.write_wav(voice, np.zeros(2 * azure.SAMPLE_RATE, dtype="<i2"))
            azure.render_video(source, bg, voice, output, 2, .55)
            self.assertGreater(output.stat().st_size, 0)

    def test_small_slowdown_covers_overrun_without_moving_later_segments(self):
        segments = [
            {"id": "s0002", "start": 6.934, "end": 9.016, "en": "Court", "zh": "球场中心"},
            {"id": "s0003", "start": 12.019, "end": 15.944, "en": "Tech", "zh": "技术"},
        ]
        timings = [
            {"id": "s0002", "audio_seconds": 2.721, "overrun_seconds": 0.289},
            {"id": "s0003", "audio_seconds": 2.12, "overrun_seconds": 0.0},
        ]
        issues = [{"id": "s0002", "kind": "overrun", "seconds": 0.289, "suggestion": "old"}]
        retiming, remaining = azure.apply_video_fit(segments, timings, issues, 30)
        self.assertEqual(remaining, [])
        self.assertEqual(retiming[0]["video_speed"], 0.878)
        self.assertEqual(retiming[0]["pause_speed"], 1.106)
        self.assertEqual(retiming[0]["next_start"], 12.019)
        self.assertEqual(retiming[0]["subtitle_end"], 9.655)
        cues = azure.retimed_segments(segments, retiming)
        self.assertEqual(cues[0]["end"], 9.655)
        self.assertEqual(cues[1]["start"], 12.019)
        pieces = azure.retime_pieces(retiming, 30)
        self.assertEqual(pieces[1]["speed"], 0.878)
        self.assertEqual(pieces[2]["speed"], 1.106)
        self.assertEqual(pieces[-1]["end"], 30)

    def test_overrun_beyond_slight_video_change_stays_in_the_report(self):
        segments = [{"id": "s1", "start": 0.0, "end": 1.0, "zh": "太长了"}]
        timings = [{"id": "s1", "audio_seconds": 1.8}]
        issues = [{"id": "s1", "kind": "overrun", "seconds": 0.5, "suggestion": "old"}]
        retiming, remaining = azure.apply_video_fit(segments, timings, issues, 4)
        self.assertEqual(retiming, [])
        self.assertIn("15%", remaining[0]["suggestion"])

    def test_overlap_blocks_video_slowdown(self):
        segments = [
            {"id": "s1", "start": 0.0, "end": 1.0, "zh": "甲"},
            {"id": "s2", "start": 1.2, "end": 2.0, "zh": "乙"},
        ]
        timings = [{"id": "s1", "audio_seconds": 1.3}, {"id": "s2", "audio_seconds": 0.5}]
        issues = [
            {"id": "s1", "kind": "overrun", "seconds": 0.1, "suggestion": "old"},
            {"id": "s1", "kind": "overlap", "seconds": 0.05, "suggestion": "重叠"},
        ]
        retiming, remaining = azure.apply_video_fit(segments, timings, issues, 4)
        self.assertEqual(retiming, [])
        self.assertEqual(len(remaining), 2)

    def test_gap_before_next_sentence_is_not_reported_as_overlap(self):
        config = {**azure.DEFAULTS, "max_global_adjust": 0,
                  "max_local_adjust": 0, "max_post_adjust": 0}
        segments = [{"id": "s1", "start": 0, "end": 1, "zh": "第一句"},
                    {"id": "s2", "start": 1.2, "end": 2, "zh": "第二句"}]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(azure, "synthesize", side_effect=[sample_wav(1.18), sample_wav(.5)]):
            _, issues, _ = azure.make_voice_track(segments, config, "fake-key", Path(directory), 3)
        self.assertTrue(any(item["kind"] == "overrun" for item in issues))
        self.assertFalse(any(item["kind"] == "overlap" for item in issues))

    def test_fixable_video_adjustment_is_preserved_with_other_blocked_sentence(self):
        segments = [
            {"id": "s1", "start": 0.0, "end": 1.0, "zh": "甲"},
            {"id": "s2", "start": 2.0, "end": 3.0, "zh": "乙"},
        ]
        timings = [{"id": "s1", "audio_seconds": 1.3},
                   {"id": "s2", "audio_seconds": 2.0}]
        issues = [{"id": "s1", "kind": "overrun", "seconds": .1, "suggestion": "old"},
                  {"id": "s2", "kind": "overrun", "seconds": .8, "suggestion": "old"}]
        retiming, remaining = azure.apply_video_fit(segments, timings, issues, 4)
        self.assertEqual([item["id"] for item in retiming], ["s1"])
        self.assertEqual([item["id"] for item in remaining], ["s2"])

    def test_ffmpeg_retiming_keeps_total_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, bg, voice, output = (root / name for name in ("source.mp4", "bg.wav", "voice.wav", "dubbed.mp4"))
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "color=c=black:s=64x64:r=24:d=2", "-c:v", "libx264", str(source)], check=True)
            azure.write_wav(bg, np.zeros(2 * azure.SAMPLE_RATE, dtype="<i2"))
            azure.write_wav(voice, np.zeros(2 * azure.SAMPLE_RATE, dtype="<i2"))
            pieces = [{"start": 0, "end": 1, "speed": 0.9}, {"start": 1, "end": 2, "speed": 1.125}]
            azure.render_video(source, bg, voice, output, 2, .55, pieces)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "default=nw=1:nk=1", str(output)],
                                   capture_output=True, text=True, check=True)
            self.assertAlmostEqual(float(probe.stdout.strip()), 2, delta=0.08)

    def test_elastic_timeline_shifts_later_voice_and_subtitles(self):
        segments = [{"id": "s1", "start": 0.5, "end": 1.5, "zh": "第一句"},
                    {"id": "s2", "start": 2.0, "end": 2.8, "zh": "第二句"}]
        timings = [{"id": "s1", "audio_seconds": 1.7},
                   {"id": "s2", "audio_seconds": .5}]
        pieces, cues, changes, issues, duration = azure.plan_elastic_timeline(
            segments, timings, 4.0, max_adjust=.18)
        self.assertEqual(issues, [])
        self.assertEqual(len(changes), 1)
        self.assertLess(changes[0]["video_speed"], 1)
        self.assertGreater(cues[1]["start"], segments[1]["start"])
        self.assertAlmostEqual(cues[1]["start"] - segments[1]["start"], duration - 4.0, delta=.002)
        self.assertEqual(timings[1]["output_start"], cues[1]["start"])
        self.assertAlmostEqual(pieces[-1]["end"], 4.0)

    def test_elastic_timeline_honors_video_limit(self):
        segments = [{"id": "s1", "start": 0, "end": 1, "zh": "第一句"},
                    {"id": "s2", "start": 1.9, "end": 2.5, "zh": "第二句"}]
        timings = [{"id": "s1", "audio_seconds": 2.23},
                   {"id": "s2", "audio_seconds": .3}]
        _, _, _, issues, _ = azure.plan_elastic_timeline(segments, timings, 3, max_adjust=.15)
        self.assertEqual([issue["kind"] for issue in issues], ["timeline_limit"])
        _, _, _, issues, _ = azure.plan_elastic_timeline(segments, timings, 3, max_adjust=.18)
        self.assertEqual(issues, [])

    def test_ffmpeg_elastic_render_extends_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, bg, voice, output = (root / name for name in ("source.mp4", "bg.wav", "voice.wav", "dubbed.mp4"))
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "color=c=black:s=64x64:r=24:d=2", "-c:v", "libx264", str(source)], check=True)
            azure.write_wav(bg, np.zeros(2 * azure.SAMPLE_RATE, dtype="<i2"))
            azure.write_wav(voice, np.zeros(round(2.5 * azure.SAMPLE_RATE), dtype="<i2"))
            subtitle = root / "dubbed-bilingual.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n你好\n", encoding="utf-8-sig")
            pieces = [{"start": 0, "end": 1, "speed": 0.8}, {"start": 1, "end": 2, "speed": 1}]
            azure.render_video(source, bg, voice, output, 2.25, .55, pieces,
                               watermark={"enabled": True, "text": "课程 ABC", "position": "top_right",
                                          "opacity": .45},
                               subtitles={"enabled": True, "mode": "bilingual", "font_size": 20,
                                          "margin_bottom": 20, "outline": 2},
                               subtitle_path=subtitle)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "default=nw=1:nk=1", str(output)],
                                   capture_output=True, text=True, check=True)
            self.assertAlmostEqual(float(probe.stdout.strip()), 2.25, delta=.08)
            self.assertFalse(output.with_suffix(".watermark.txt").exists())

    def test_ffmpeg_watermark_without_retiming_reencodes_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, bg, voice, output = (root / name for name in ("source.mp4", "bg.wav", "voice.wav", "dubbed.mp4"))
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "color=c=black:s=320x180:r=24:d=1", "-c:v", "libx264", str(source)], check=True)
            azure.write_wav(bg, np.zeros(azure.SAMPLE_RATE, dtype="<i2"))
            azure.write_wav(voice, np.zeros(azure.SAMPLE_RATE, dtype="<i2"))
            azure.render_video(source, bg, voice, output, 1, .55,
                               watermark={"enabled": True, "text": "课程 ABC", "position": "top_left",
                                          "opacity": .8})
            frame = root / "frame.rgb"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(output),
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", str(frame)], check=True)
            pixels = np.frombuffer(frame.read_bytes(), dtype=np.uint8)
            self.assertGreater(int(pixels.max()), 30)

    def test_ffmpeg_burns_final_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, bg, voice, output = (root / name for name in ("source.mp4", "bg.wav", "voice.wav", "dubbed.mp4"))
            subtitle = root / "dubbed-zh.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,500\n中文字幕\n", encoding="utf-8-sig")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "color=c=black:s=320x180:r=24:d=2", "-c:v", "libx264", str(source)], check=True)
            azure.write_wav(bg, np.zeros(2 * azure.SAMPLE_RATE, dtype="<i2"))
            azure.write_wav(voice, np.zeros(2 * azure.SAMPLE_RATE, dtype="<i2"))
            azure.render_video(source, bg, voice, output, 2, .55,
                               subtitles={"enabled": True, "mode": "zh", "font_size": 28,
                                          "margin_bottom": 30, "outline": 2},
                               subtitle_path=subtitle)
            frame = root / "subtitle.rgb"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.7",
                            "-i", str(output), "-frames:v", "1", "-f", "rawvideo",
                            "-pix_fmt", "rgb24", str(frame)], check=True)
            self.assertGreater(int(np.frombuffer(frame.read_bytes(), dtype=np.uint8).max()), 30)

    def test_edit_chinese_regenerates_subtitles_and_invalidates_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(web_app, "DATA", root), patch.object(web_app, "DB", root / "jobs.sqlite3"):
                web_app.initialize()
                with web_app.connect() as conn:
                    conn.execute("""INSERT INTO jobs
                        (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at,translation_status,dubbing_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("test", "Test", "", "", "test.mp4", "", None, "prepared", "prepared", "", "", "", "done", "done"))
                (root / "test-translation.json").write_text(json.dumps({"groups": [{"segments": [
                    {"id": "s1", "start": .2, "end": 1.2, "en": "Hello", "zh": "你好", "flags": []}]}]}), encoding="utf-8")
                (root / "test-voice.wav").write_bytes(b"old voice")
                web_app.edit_chinese("test", "s1", web_app.ChineseEdit(zh="大家好"))
                self.assertIn("大家好", (root / "test-bilingual.srt").read_text(encoding="utf-8-sig"))
                self.assertFalse((root / "test-voice.wav").exists())
                self.assertEqual(web_app.get_job("test")["dubbing_status"], "not_started")


if __name__ == "__main__":
    unittest.main()
