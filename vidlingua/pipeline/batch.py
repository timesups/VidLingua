"""Prepare local batch inputs and publish finished videos."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path


VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".ts"}


def scan_directory(directory: Path) -> list[Path]:
    directory = directory.expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("请输入视频所在的文件夹路径")
    videos = sorted((item for item in directory.iterdir()
                     if item.is_file() and item.suffix.lower() in VIDEO_SUFFIXES),
                    key=lambda item: item.name.casefold())
    if not videos:
        raise ValueError("文件夹当前层没有支持的视频文件")
    return videos


def prepare_video(source: Path, target: Path, log_path: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"源视频不存在：{source}")
    if target.is_file() and target.stat().st_size:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.mp4")
    try:
        if source.suffix.lower() == ".mp4":
            shutil.copy2(source, temporary)
        else:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"批处理：正在转换 {source.name} 为 MP4\n")
                result = subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
                    "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    str(temporary),
                ], stdout=log, stderr=subprocess.STDOUT, check=False)
            if result.returncode:
                raise RuntimeError(f"视频转换失败，退出码 {result.returncode}；请查看任务日志")
        if not temporary.is_file() or not temporary.stat().st_size:
            raise RuntimeError("批处理未生成可用的 MP4 视频")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def publish_video(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
