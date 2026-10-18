"""Import a video, optionally separate vocals, then transcribe it with WhisperX."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .import_media import import_media
from .separate_audio import separate


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--separate-vocals", action="store_true", help="Use separated vocals for the main transcript")
    parser.add_argument("--compare-original", action="store_true", help="Also transcribe the original mix")
    args = parser.parse_args()

    try:
        job_dir = import_media(args.video, PROJECT_ROOT / "runs")
        print(f"Imported: {job_dir}", flush=True)
        if args.separate_vocals:
            print(f"Separated ASR audio: {separate(job_dir)}", flush=True)
        command = [sys.executable, "-m", "vidlingua.pipeline.transcribe", str(job_dir)]
        if args.separate_vocals:
            command += ["--audio-source", "vocals"]
        elif (job_dir / "asr_vocals.wav").is_file():
            command += ["--audio-source", "original"]
        result = subprocess.run(command, check=False)
        if result.returncode:
            return result.returncode
        if args.compare_original and args.separate_vocals:
            result = subprocess.run([
                sys.executable, "-m", "vidlingua.pipeline.transcribe",
                str(job_dir), "--audio-source", "original",
            ], check=False)
            if result.returncode:
                return result.returncode
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Processing failed: {exc}\n")
    print(f"Done: {job_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
