"""Diagnostics explain why an ASR gap was or was not repaired."""

import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from vidlingua.pipeline.transcribe import recover_transcript_gaps


class TranscribeLoggingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audio = self.root / "asr_vocals.wav"
        self.transcript = self.root / "asr_vocals.json"
        self.transcript.write_text(json.dumps({"language": "en", "segments": [
            {"start": 0.0, "end": 1.0, "text": "Hello.", "words": []},
            {"start": 8.0, "end": 9.0, "text": "Goodbye.", "words": []},
        ]}), encoding="utf-8")

    def write_audio(self, volume):
        samples = np.full(10 * 16000, volume, dtype="<i2")
        with wave.open(str(self.audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(samples.tobytes())

    def test_quiet_gap_records_rejection_without_loading_model(self):
        self.write_audio(0)
        messages = []
        with patch("faster_whisper.WhisperModel") as model:
            self.assertEqual(recover_transcript_gaps(self.audio, self.transcript, messages.append), 0)
            model.assert_not_called()
        report = json.loads(self.transcript.with_suffix(".diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(report["gaps"][0]["decision"], "quiet")
        self.assertEqual(report["recovered_segment_count"], 0)
        self.assertIn("空白 1.00–8.00s", "\n".join(messages))

    def test_recovered_gap_records_model_output_and_final_text(self):
        self.write_audio(4000)
        words = [SimpleNamespace(word=" missing", start=2.0, end=2.5),
                 SimpleNamespace(word=" sentence.", start=2.5, end=3.0)]
        result = SimpleNamespace(start=2.0, end=3.0, text=" missing sentence.",
                                 avg_logprob=-0.2, no_speech_prob=0.01, words=words)
        messages = []
        with patch("faster_whisper.WhisperModel") as model:
            model.return_value.transcribe.return_value = ([result], None)
            self.assertEqual(recover_transcript_gaps(self.audio, self.transcript, messages.append), 1)
        report = json.loads(self.transcript.with_suffix(".diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(report["gaps"][0]["recognition"][0]["decision"], "accepted")
        self.assertEqual(report["gaps"][0]["kept_segments"][0]["text"], "missing sentence.")
        self.assertEqual(report["recovered_segment_count"], 1)
        original = json.loads(self.transcript.with_suffix(".raw.json").read_text(encoding="utf-8"))
        self.assertEqual(len(original["segments"]), 2)
        self.assertIn("补回 1 段", "\n".join(messages))
        self.assertIn("missing sentence.", self.transcript.with_suffix(".srt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
