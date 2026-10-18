"""Checks for readable subtitles without losing aligned transcript words."""

import json
import tempfile
import unittest
from pathlib import Path

from vidlingua.pipeline.preparation import make_segments


class PreparationTests(unittest.TestCase):
    def make_transcript(self, sources):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "asr.json"
        path.write_text(json.dumps({"language": "en", "segments": sources}), encoding="utf-8")
        return path

    def test_long_aligned_sentence_splits_without_losing_words(self):
        tokens = ("This lesson explains how the engine stores data, then retrieves it when needed "
                  "and shows why the result stays consistent.").split()
        words = [{"word": word, "start": index * 0.55, "end": index * 0.55 + 0.4, "score": 0.9}
                 for index, word in enumerate(tokens)]
        source = {"start": 0, "end": words[-1]["end"], "text": " ".join(tokens), "words": words}
        segments = make_segments(self.make_transcript([source]), 20)
        self.assertGreater(len(segments), 1)
        self.assertEqual([word for segment in segments for word in segment["words"]], words)
        self.assertEqual(" ".join(segment["edited_text"] for segment in segments), source["text"])
        self.assertTrue(all(segment["end"] - segment["start"] <= 9 for segment in segments))

    def test_long_sentence_prefers_clause_boundary_over_article(self):
        tokens = ("GTA 4 hadn't even celebrated its first ever birthday when Rockstar had already "
                  "begun laying the earliest foundations for the next title.").split()
        words = [{"word": word, "start": index * .4, "end": index * .4 + .3}
                 for index, word in enumerate(tokens)]
        source = {"start": 0, "end": words[-1]["end"], "text": " ".join(tokens), "words": words}
        segments = make_segments(self.make_transcript([source]), 12)
        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0]["edited_text"].endswith("birthday"))
        self.assertTrue(segments[1]["edited_text"].startswith("when Rockstar"))
        self.assertEqual(" ".join(item["edited_text"] for item in segments), source["text"])

    def test_punctuation_wins_over_nearer_plain_word_boundary(self):
        tokens = ("First we explain the concept clearly. Then we demonstrate how the system "
                  "works with several practical examples and more detail.").split()
        words = [{"word": word, "start": index * .48, "end": index * .48 + .35}
                 for index, word in enumerate(tokens)]
        source = {"start": 0, "end": words[-1]["end"], "text": " ".join(tokens), "words": words}
        segments = make_segments(self.make_transcript([source]), 15)
        self.assertGreater(len(segments), 1)
        self.assertTrue(segments[0]["edited_text"].endswith("clearly."))

    def test_short_segment_keeps_original_timing(self):
        source = {"start": 0, "end": 4, "text": "A short sentence.",
                  "words": [{"word": "A", "start": 0.5, "end": 0.7},
                            {"word": "short", "start": 1, "end": 1.4},
                            {"word": "sentence.", "start": 2, "end": 2.6}]}
        segments = make_segments(self.make_transcript([source]), 10)
        self.assertEqual((segments[0]["start"], segments[0]["end"]), (0, 4))

    def test_missing_word_timing_does_not_guess_split_points(self):
        source = {"start": 0, "end": 15, "text": "A long sentence with no reliable timing.",
                  "words": [{"word": "A"}]}
        segments = make_segments(self.make_transcript([source]), 20)
        self.assertEqual(len(segments), 1)
        self.assertIn("missing_word_time", segments[0]["flags"])
        self.assertIn("long_segment", segments[0]["flags"])

    def test_merge_respects_subtitle_duration_limit(self):
        sources = [
            {"start": 0, "end": 6, "text": "First clause,", "words": []},
            {"start": 6.2, "end": 12, "text": "second clause.", "words": []},
        ]
        segments = make_segments(self.make_transcript(sources), 20)
        self.assertEqual(len(segments), 2)


if __name__ == "__main__":
    unittest.main()
