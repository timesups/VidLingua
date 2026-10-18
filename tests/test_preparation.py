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
        words = [{"word": word, "start": index * .5, "end": index * .5 + .4}
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

    def aligned_source(self, text, duration=7):
        tokens = text.split()
        step = duration / len(tokens)
        words = [{"word": word, "start": i * step, "end": (i + 1) * step - .01}
                 for i, word in enumerate(tokens)]
        return {"start": 0, "end": duration, "text": text, "words": words}

    def test_fast_complete_sentences_are_not_cut_at_character_target(self):
        examples = [
            "The physics plugin I am talking about is called Kawaii Physics and was made by a developer who goes by the handle of Okazu.",
            "Of course you could then spend a lot of time playing around with these physics setting values to tweak the movement to your liking.",
            "You can easily add the ability to be affected by wind and gravity to your target bones by just hitting a check mark.",
            "And yeah, the awesome shader that comes with this map definitely does a lot of the heavy lifting of making this look anime.",
        ]
        for text in examples:
            with self.subTest(text=text):
                source = self.aligned_source(text)
                result = make_segments(self.make_transcript([source]), 20)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["edited_text"], text)

    def test_very_long_unpunctuated_text_is_bounded_and_lossless(self):
        source = self.aligned_source(" ".join(["word"] * 120), 40)
        result = make_segments(self.make_transcript([source]), 41)
        self.assertEqual([w for s in result for w in s["words"]], source["words"])
        self.assertTrue(all(s["end"] - s["start"] <= 12 for s in result))
        self.assertTrue(all(len(s["edited_text"]) <= 240 for s in result))

    def test_null_alignment_preserves_source(self):
        source = self.aligned_source(" ".join(["word"] * 60), 20)
        source["words"][3]["start"] = None
        result = make_segments(self.make_transcript([source]), 21)
        self.assertEqual(len(result), 1)

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
