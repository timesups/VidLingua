"""Separate speech from background audio for an imported VidLingua job."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"


def run(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout


def probe_duration(path: Path) -> float:
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    return float(json.loads(result)["format"]["duration"])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def separate(job_dir: Path) -> Path:
    job_dir = job_dir.resolve()
    source = job_dir / "source_audio.flac"
    if not source.is_file():
        raise FileNotFoundError(f"Missing imported audio: {source}")
    output = job_dir / "separated"
    output.mkdir(exist_ok=True)
    vocals = output / "vocals.flac"
    instrumental = output / "instrumental.flac"
    asr_vocals = job_dir / "asr_vocals.wav"
    metadata_path = output / "metadata.json"
    source_hash = sha256_file(source)

    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (metadata.get("source_audio_sha256") == source_hash
                and metadata.get("model") == MODEL
                and all(path.is_file() for path in (vocals, instrumental, asr_vocals))):
            return asr_vocals
        raise RuntimeError(f"Existing separation metadata does not match input or files: {metadata_path}")

    if not vocals.is_file() or not instrumental.is_file():
        raw_vocals = list(output.glob("source_audio_(Vocals)_*.flac"))
        raw_instrumental = list(output.glob("source_audio_(Instrumental)_*.flac"))
        if len(raw_vocals) != 1 or len(raw_instrumental) != 1:
            cli = Path(sys.executable).with_name("audio-separator.exe")
            if not cli.is_file():
                raise FileNotFoundError(f"Audio Separator is not installed in this environment: {cli}")
            subprocess.run([
                str(cli), str(source), "--model_filename", MODEL,
                "--output_dir", str(output),
                "--model_file_dir", str(PROJECT_ROOT / "models" / "separator"),
                "--output_format", "FLAC",
            ], check=True)
            raw_vocals = list(output.glob("source_audio_(Vocals)_*.flac"))
            raw_instrumental = list(output.glob("source_audio_(Instrumental)_*.flac"))
        if len(raw_vocals) != 1 or len(raw_instrumental) != 1:
            raise RuntimeError("Expected one vocals stem and one instrumental stem")
        raw_vocals[0].replace(vocals)
        raw_instrumental[0].replace(instrumental)

    source_duration = probe_duration(source)
    vocals_duration = probe_duration(vocals)
    instrumental_duration = probe_duration(instrumental)
    if max(abs(source_duration - vocals_duration), abs(source_duration - instrumental_duration)) > 0.05:
        raise RuntimeError("Separated stems differ from source duration by more than 50 ms")

    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(vocals), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(asr_vocals),
    ], check=True)
    metadata_path.write_text(json.dumps({
        "model": MODEL,
        "source_audio_sha256": source_hash,
        "source_duration_seconds": source_duration,
        "vocals_duration_seconds": vocals_duration,
        "instrumental_duration_seconds": instrumental_duration,
        "artifacts": {"vocals": "vocals.flac", "instrumental": "instrumental.flac", "asr_vocals": "../asr_vocals.wav"},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return asr_vocals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path, help="Directory created by import_media.py")
    args = parser.parse_args()
    try:
        print(separate(args.job_dir))
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Separation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
