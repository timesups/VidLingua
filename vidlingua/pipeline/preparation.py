"""Turn a vocals-only WhisperX transcript into reviewable translation segments."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path


# Translation / dubbing units are semantic chunks, not display line widths.
MAX_SEGMENT_SECONDS = 9.0  # Soft target: a complete sentence may exceed this.
MAX_SEGMENT_CHARS = 110  # Soft target only.
HARD_SEGMENT_SECONDS = 12.0
HARD_SEGMENT_CHARS = 240
CLAUSE_STARTERS = {"when", "while", "because", "although", "though", "whereas",
                   "unless", "until", "after", "before", "once", "if", "but", "however"}
WEAK_ENDINGS = {"a", "an", "the", "to", "of", "for", "in", "on", "at", "with", "by",
                "and", "or", "but", "when", "while", "because", "your", "my", "our",
                "their", "its", "this", "that", "these", "those", "through"}
WEAK_STARTS = {"of", "to", "by", "for", "with", "through"}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _aligned(source: dict) -> bool:
    words = source.get("words") or []
    return bool(words) and all(
        isinstance(w.get("start"), (int, float)) and isinstance(w.get("end"), (int, float))
        and w["end"] >= w["start"] for w in words
    ) and all(words[i]["start"] >= words[i - 1]["start"] for i in range(1, len(words))) and (
        clean_text(source.get("text", "")) == clean_text(" ".join(w["word"] for w in words)))


def _boundary_cost(words: list[dict], right: int) -> float:
    """Lower is better; punctuation outranks pauses and lexical hints."""
    ending = words[right]["word"].strip()
    if re.search(r"[.!?][\"']?$", ending):
        return 0.0
    if re.search(r"[;:][\"']?$", ending):
        return 2.0
    if re.search(r",[\"']?$", ending):
        return 4.0
    current = ending.lower().strip("\"'.,;:!?")
    following = words[right + 1]["word"].lower().strip("\"'.,;:!?(")
    pause = max(0.0, words[right + 1]["start"] - words[right]["end"])
    cost = 24.0
    if following in CLAUSE_STARTERS:
        cost = 7.0
    elif following in {"and", "or"}:
        cost = 12.0
    if pause >= .5:
        cost = min(cost, 10.0 - min(pause, 1.5) * 2)
    if current in WEAK_ENDINGS or following in WEAK_STARTS:
        cost += 30.0
    return cost


def _split_source(source: dict) -> list[dict]:
    """Choose the whole partition, avoiding greedy cuts and tiny final tails.

    Keep normal-duration sentences intact regardless of subtitle line width.
    For longer spans prefer linguistic boundaries, allowing up to 12 seconds.
    Unreliable alignment is preserved for review rather than guessed.
    """
    text = clean_text(source.get("text", ""))
    if (float(source.get("end", 0)) - float(source.get("start", 0)) <= MAX_SEGMENT_SECONDS
            and len(text) <= HARD_SEGMENT_CHARS):
        return [source]
    if not _aligned(source):
        return [source]
    words = source["words"]
    count = len(words)
    costs = [float("inf")] * (count + 1)
    ends = [count] * count
    costs[count] = 0.0
    for left in range(count - 1, -1, -1):
        chars = 0
        for right in range(left, count):
            chars += len(words[right]["word"]) + (right > left)
            seconds = words[right]["end"] - words[left]["start"]
            # An indivisible word is retained even when its timing is abnormal.
            if right > left and (seconds > HARD_SEGMENT_SECONDS or chars > HARD_SEGMENT_CHARS):
                break
            penalty = 4.0 + max(0, seconds - MAX_SEGMENT_SECONDS) ** 2 * 3
            if seconds > MAX_SEGMENT_SECONDS:
                penalty += 20.0
            penalty += max(0, chars - MAX_SEGMENT_CHARS) / 60
            penalty += max(0, 2.0 - seconds) * 8
            if right < count - 1:
                penalty += _boundary_cost(words, right)
            candidate = penalty + costs[right + 1]
            if candidate < costs[left]:
                costs[left], ends[left] = candidate, right + 1
    chunks = []
    left = 0
    while left < count:
        right = ends[left]
        selected = words[left:right]
        chunks.append({**source,
                       "start": float(selected[0]["start"]) if left else source["start"],
                       "end": float(selected[-1]["end"]) if right < count else source["end"],
                       "text": clean_text(" ".join(word["word"] for word in selected)),
                       "words": selected})
        left = right
    return chunks


def make_segments(transcript_path: Path, duration: float) -> list[dict]:
    data = json.loads(transcript_path.read_text(encoding="utf-8"))
    if data.get("language") != "en":
        raise ValueError("Expected an English WhisperX transcript")
    sources = []
    for source in data.get("segments", []):
        if (sources and sources[-1].get("text", "").strip()
                and not re.search(r"[.!?][\"']?$", sources[-1]["text"].strip())
                and float(source.get("start", 0)) - float(sources[-1].get("end", 0)) <= 0.7
                and float(source.get("end", 0)) - float(sources[-1].get("start", 0)) <= HARD_SEGMENT_SECONDS
                and _aligned(sources[-1]) and _aligned(source)
                and len(clean_text(sources[-1]["text"] + " " + source.get("text", ""))) <= HARD_SEGMENT_CHARS):
            previous = sources[-1]
            previous["end"] = source["end"]
            previous["text"] = clean_text(previous["text"] + " " + source.get("text", ""))
            previous["words"] = (previous.get("words") or []) + (source.get("words") or [])
        else:
            sources.append(dict(source))
    sources = [part for source in sources for part in _split_source(source)]
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
        if not words or any(not isinstance(w.get("start"), (int, float)) or not isinstance(w.get("end"), (int, float)) for w in words):
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
    root = Path(__file__).resolve().parents[2]
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
