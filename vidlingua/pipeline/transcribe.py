"""Run WhisperX on an imported VidLingua job."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path, help="Directory created by import_media.py")
    parser.add_argument("--model", default="small", help="Whisper model size (default: small)")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--audio-source", choices=("auto", "original", "vocals"), default="auto",
                        help="auto prefers separated vocals when available")
    args = parser.parse_args()

    job_dir = args.job_dir.resolve()
    selected_source = args.audio_source
    if selected_source == "auto":
        separated_ready = (job_dir / "asr_vocals.wav").is_file() and (job_dir / "separated" / "metadata.json").is_file()
        selected_source = "vocals" if separated_ready else "original"
    audio_path = job_dir / ("asr_vocals.wav" if selected_source == "vocals" else "asr_audio.wav")
    punkt_path = PROJECT_ROOT / "models" / "nltk" / "tokenizers" / "punkt_tab" / "english"
    if not audio_path.is_file():
        parser.error(f"Missing imported audio: {audio_path}")
    if not punkt_path.is_dir():
        parser.error(f"Missing NLTK punkt_tab data: {punkt_path}")

    environment = os.environ.copy()
    environment["HF_HOME"] = str(PROJECT_ROOT / "models" / "huggingface")
    environment["NLTK_DATA"] = str(PROJECT_ROOT / "models" / "nltk")
    environment["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    command = [
        sys.executable, "-m", "whisperx", str(audio_path),
        "--model", args.model,
        "--model_dir", str(PROJECT_ROOT / "models" / "whisper"),
        "--language", "en",
        "--device", "cuda",
        "--compute_type", "float16",
        "--batch_size", str(args.batch_size),
        "--vad_method", "silero",
        "--chunk_size", "15",
        "--output_dir", str(job_dir / ("transcript" if args.audio_source == "auto" else f"transcript_{selected_source}")),
        "--output_format", "all",
        "--verbose", "False",
        "--print_progress", "True",
    ]
    return subprocess.run(command, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
