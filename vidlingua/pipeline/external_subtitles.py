"""Import a same-named English SRT/VTT file as a batch transcript."""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path


TIMING = re.compile(
    r"(?P<start>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})(?:\s|$)"
)


def matching_subtitle(video: Path) -> Path | None:
    candidates = {item.suffix.casefold(): item for item in video.parent.iterdir()
                  if item.is_file() and item.stem.casefold() == video.stem.casefold()
                  and item.suffix.casefold() in {".srt", ".vtt"}}
    return candidates.get(".srt") or candidates.get(".vtt")


def _seconds(stamp: str) -> float:
    parts = stamp.replace(",", ".").split(":")
    seconds = float(parts[-1]) + int(parts[-2]) * 60
    if len(parts) == 3:
        seconds += int(parts[0]) * 3600
    return seconds


def parse_subtitles(source: Path) -> list[dict]:
    raw = source.read_bytes()
    text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    segments = []
    for block in blocks:
        lines = block.strip().splitlines()
        for index, line in enumerate(lines):
            match = TIMING.search(line)
            if not match:
                continue
            start, end = _seconds(match["start"]), _seconds(match["end"])
            content = " ".join(lines[index + 1:])
            content = html.unescape(re.sub(r"<[^>]*>|\{\\[^}]*\}", "", content))
            content = re.sub(r"\s+", " ", content).strip()
            if content and end > start:
                segments.append({"start": start, "end": end, "text": content,
                                 "words": [], "origin": "external_subtitle"})
            break
    if not segments:
        raise ValueError("字幕文件没有可用的时间轴和台词")
    content = " ".join(segment["text"] for segment in segments)
    if not re.search(r"[A-Za-z]", content) or len(re.findall(r"[\u4e00-\u9fff]", content)) > len(re.findall(r"[A-Za-z]", content)):
        raise ValueError("同名字幕看起来不是英文字幕")
    return segments


def import_subtitle_transcript(source: Path, destination: Path) -> bool:
    """Return True when an existing import with the same source hash was reused."""
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if destination.is_file():
        try:
            saved = json.loads(destination.read_text(encoding="utf-8"))
            if saved.get("external_subtitle", {}).get("sha256") == digest:
                return True
        except (OSError, ValueError, TypeError):
            pass
    segments = parse_subtitles(source)
    payload = {"language": "en", "segments": segments,
               "external_subtitle": {"path": str(source.resolve()), "sha256": digest}}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".importing.json")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return False
