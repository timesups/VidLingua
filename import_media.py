"""Import a video into an immutable, content-addressed working directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command is unavailable: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{exc.stderr[-3000:]}") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path: Path) -> dict:
    result = run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-show_chapters", "-of", "json", str(path),
    ])
    return json.loads(result.stdout)


def extract_audio(source: Path, destination: Path, asr: bool) -> None:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", str(source), "-map", "0:a:0", "-vn"]
    if asr:
        command += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
    else:
        command += ["-c:a", "flac"]
    run(command + [str(destination)])


def import_media(source: Path, output_root: Path) -> Path:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"Not a file: {source}")
    source_hash = sha256_file(source)
    job_id = f"{source.stem}-{source_hash[:12]}"
    local_source_name = f"source{source.suffix.lower()}"
    destination = output_root.resolve() / job_id
    manifest_path = destination / "manifest.json"
    if destination.exists():
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            required = [local_source_name, "probe.json", "source_audio.flac", "asr_audio.wav"]
            if (manifest.get("source", {}).get("sha256") == source_hash
                    and all((destination / name).is_file() for name in required)):
                return destination
        raise RuntimeError(f"Existing import is incomplete; inspect it before retrying: {destination}")

    metadata = probe(source)
    streams = metadata.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not videos or not audios:
        raise ValueError("The input must contain at least one video stream and one audio stream")

    output_root.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        copied_source = destination / local_source_name
        shutil.copy2(source, copied_source)
        (destination / "probe.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        extract_audio(copied_source, destination / "source_audio.flac", asr=False)
        extract_audio(copied_source, destination / "asr_audio.wav", asr=True)
        manifest = {
            "schema_version": 1,
            "job_id": job_id,
            "imported_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": {
                "original_path": str(source), "sha256": source_hash,
                "size_bytes": source.stat().st_size, "local_file": local_source_name,
            },
            "media": {
                "duration_seconds": float(metadata["format"]["duration"]),
                "video_stream_count": len(videos), "audio_stream_count": len(audios),
                "primary_video": {
                    "stream_index": videos[0]["index"], "codec": videos[0].get("codec_name"),
                    "width": videos[0].get("width"), "height": videos[0].get("height"),
                    "frame_rate": videos[0].get("avg_frame_rate"),
                },
                "primary_audio": {
                    "stream_index": audios[0]["index"], "codec": audios[0].get("codec_name"),
                    "sample_rate": int(audios[0]["sample_rate"]),
                    "channels": audios[0].get("channels"),
                    "language_tag": audios[0].get("tags", {}).get("language"),
                },
            },
            "artifacts": {
                "probe": "probe.json", "source_audio": "source_audio.flac",
                "asr_audio": "asr_audio.wav",
            },
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        # Keep partial artifacts for diagnosis; an incomplete import is never reused.
        raise
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Input video, for example demo.mp4")
    parser.add_argument("--output-root", type=Path, default=Path("runs"))
    args = parser.parse_args()
    try:
        destination = import_media(args.source, args.output_root)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
