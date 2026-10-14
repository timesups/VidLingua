"""Turn a vocals-only WhisperX transcript into reviewable translation segments."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def make_segments(transcript_path: Path, duration: float) -> list[dict]:
    data = json.loads(transcript_path.read_text(encoding="utf-8"))
    if data.get("language") != "en":
        raise ValueError("Expected an English WhisperX transcript")
    sources = []
    for source in data.get("segments", []):
        if (sources and sources[-1].get("text", "").strip()
                and not re.search(r"[.!?][\"']?$", sources[-1]["text"].strip())
                and float(source.get("start", 0)) - float(sources[-1].get("end", 0)) <= 0.7
                and float(source.get("end", 0)) - float(sources[-1].get("start", 0)) <= 12):
            previous = sources[-1]
            previous["end"] = source["end"]
            previous["text"] = clean_text(previous["text"] + " " + source.get("text", ""))
            previous["words"] = (previous.get("words") or []) + (source.get("words") or [])
        else:
            sources.append(dict(source))
    results = []
    for index, source in enumerate(sources, 1):
        start, end = float(source.get("start", 0)), float(source.get("end", 0))
        raw = source.get("text", "")
        words = source.get("words") or []
        flags = []
        if not clean_text(raw):
            flags.append("empty_text")
        if start < 0 or end <= start or end > duration + 0.1:
            flags.append("invalid_time")
        if end - start > 12:
            flags.append("long_segment")
        if not words or any("start" not in w or "end" not in w for w in words):
            flags.append("missing_word_time")
        if any(w.get("score", 1) < 0.55 for w in words):
            flags.append("low_alignment_score")
        results.append({"id": f"s{index:04d}", "start": start, "end": end,
                        "raw_text": raw, "edited_text": clean_text(raw),
                        "words": words, "flags": flags, "reviewed": False,
                        "needs_realign": False})
    return results


def term_candidates(segments: list[dict]) -> list[str]:
    # Repetition is a prompt for review, never an automatic correction.
    stop = {"this", "that", "with", "from", "have", "been", "your", "there",
            "would", "could", "about", "they", "their", "what", "when", "where"}
    counts = Counter(word.lower() for segment in segments
                     for word in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", segment["edited_text"])
                     if word.lower() not in stop)
    return [word for word, count in counts.most_common(20) if count >= 2]


def realign_edits(segments: list[dict], audio_path: Path) -> None:
    changed = [segment for segment in segments if segment["needs_realign"]]
    if not changed:
        return
    root = Path(__file__).resolve().parent
    os.environ["NLTK_DATA"] = str(root / "models" / "nltk")
    os.environ["HF_HOME"] = str(root / "models" / "huggingface")
    import whisperx

    model, metadata = whisperx.load_align_model(language_code="en", device="cuda")
    audio = whisperx.load_audio(str(audio_path))
    for segment in changed:
        aligned = whisperx.align([{"start": segment["start"], "end": segment["end"],
                                   "text": segment["edited_text"]}], model, metadata,
                                 audio, "cuda", return_char_alignments=False)
        result = aligned.get("segments", [{}])[0]
        words = result.get("words", [])
        if not words or any("start" not in word or "end" not in word for word in words):
            raise ValueError(f"Could not align edited text in {segment['id']}")
        segment["words"] = words
        segment["needs_realign"] = False
        segment["flags"] = [flag for flag in segment["flags"] if flag != "missing_word_time"]


def translation_input(job: dict, segments: list[dict]) -> dict:
    glossary = [line.strip() for line in job["glossary"].splitlines() if line.strip()]
    groups: list[dict] = []
    for segment in segments:
        previous = groups[-1]["segments"][-1] if groups else None
        if not groups or len(groups[-1]["segments"]) >= 6 or (previous and segment["start"] - previous["end"] > 2.5):
            context = groups[-1]["segments"][-1]["edited_text"] if groups else ""
            groups.append({"id": f"g{len(groups)+1:03d}", "previous_context": context, "segments": []})
        groups[-1]["segments"].append({
            "id": segment["id"], "start": segment["start"], "end": segment["end"],
            "duration_seconds": round(segment["end"] - segment["start"], 3),
            "raw_text": segment["raw_text"], "edited_text": segment["edited_text"],
            "review_flags": segment["flags"],
        })
    return {
        "schema_version": 1, "job_id": job["id"], "title": job["title"],
        "subject": job["subject"], "source_language": "en", "target_language": "zh-CN",
        "audio_source": "separated_vocals" if job.get("separate_vocals", True) else "original_audio",
        "glossary": glossary,
        "translation_rules": [
            "Return the same segment IDs in the same order.",
            "Translate for natural spoken Mandarin while preserving technical meaning.",
            "Use the glossary consistently and respect each segment's time budget.",
            "Do not invent inaudible words; flag uncertainty explicitly.",
        ],
        "groups": groups,
    }
