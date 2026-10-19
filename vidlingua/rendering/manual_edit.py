"""Cut and reorder a finished video, keeping its separate subtitle files aligned."""

from __future__ import annotations

import math
import re
import subprocess
import uuid
from pathlib import Path


def duration_seconds(video: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        duration = float(result.stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("无法读取成片时长；请检查 FFprobe 和视频文件") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("成片时长无效")
    return duration


def validate_ranges(ranges: list[dict], duration: float) -> list[dict]:
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 100:
        raise ValueError("请设置 1–100 个保留片段")
    clean = []
    for index, item in enumerate(ranges, 1):
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {index} 个片段的时间无效") from exc
        if not all(math.isfinite(x) for x in (start, end)) or start < 0 or end - start < .1 or end > duration + .05:
            raise ValueError(f"第 {index} 个片段超出视频时长，或短于 0.1 秒")
        clean.append({"start": round(start, 3), "end": round(min(end, duration), 3)})
    return clean


def source_id(video: Path) -> str:
    stat = video.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _parse_time(value: str) -> float:
    hours, minutes, seconds, millis = map(int, re.split("[:,]", value))
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _format_time(value: float) -> str:
    millis = max(0, round(value * 1000))
    hours, remainder = divmod(millis, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def retime_srt(source: str, ranges: list[dict]) -> str:
    pattern = re.compile(r"(?m)^(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*$")
    cues = []
    for block in re.split(r"\n\s*\n", source.lstrip("\ufeff").replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        position = next((i for i, line in enumerate(lines) if pattern.fullmatch(line)), None)
        if position is not None:
            match = pattern.fullmatch(lines[position])
            cues.append((_parse_time(match[1]), _parse_time(match[2]), "\n".join(lines[position + 1:])))
    result = []
    offset = 0.0
    for clip in ranges:
        start, end = clip["start"], clip["end"]
        for cue_start, cue_end, content in cues:
            cut_start, cut_end = max(start, cue_start), min(end, cue_end)
            if cut_end - cut_start >= .01:
                result.append((offset + cut_start - start, offset + cut_end - start, content))
        offset += end - start
    return "\n\n".join(f"{i}\n{_format_time(start)} --> {_format_time(end)}\n{text}"
                       for i, (start, end, text) in enumerate(result, 1)) + ("\n" if result else "")


def render_edit(source: Path, target: Path, ranges: list[dict], log_path: Path | None = None) -> str:
    """Encode all selected intervals in order; replace target only after success."""
    parts = []
    for i, clip in enumerate(ranges):
        start, end = clip["start"], clip["end"]
        parts += [f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]",
                  f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]"]
    joins = "".join(f"[v{i}][a{i}]" for i in range(len(ranges)))
    parts.append(f"{joins}concat=n={len(ranges)}:v=1:a=1[outv][outa]")
    filter_path = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.filter")
    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.mp4")
    filter_path.write_text(";\n".join(parts), encoding="utf-8")
    errors = []
    try:
        for encoder in ("h264_nvenc", "libx264"):
            options = (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "18", "-b:v", "0"]
                       if encoder == "h264_nvenc" else
                       ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"])
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-i", str(source), "-filter_complex_script", str(filter_path),
                       "-map", "[outv]", "-map", "[outa]", *options, "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-movflags", "+faststart", str(temporary)]
            if log_path:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"手动剪辑：使用 {encoder} 导出 {len(ranges)} 个片段\n")
            try:
                result = subprocess.run(command, capture_output=True, text=True, check=False)
            except FileNotFoundError as exc:
                raise RuntimeError("未找到 FFmpeg，无法导出剪辑视频") from exc
            if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size:
                temporary.replace(target)
                return encoder
            errors.append(f"{encoder}: {result.stderr[-500:]}")
            temporary.unlink(missing_ok=True)
        raise RuntimeError("剪辑导出失败：" + "；".join(errors))
    finally:
        filter_path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
