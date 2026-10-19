"""Run WhisperX on an imported VidLingua job."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GAP_MIN_SECONDS = 4.0
GAP_MAX_SECONDS = 30.0
GAP_MIN_RMS = 0.012


def _gap_scan(segments: list[dict], audio: "np.ndarray", sample_rate: int) -> list[dict]:
    """Keep the measurements behind each recovery decision for later diagnosis."""
    import numpy as np

    checks = []
    for previous, following in zip(segments, segments[1:]):
        start, end = float(previous["end"]), float(following["start"])
        duration = end - start
        if duration < GAP_MIN_SECONDS:
            continue
        check = {"start": round(start, 3), "end": round(end, 3),
                 "duration": round(duration, 3), "previous_text": previous.get("text", "").strip(),
                 "following_text": following.get("text", "").strip()}
        if duration > GAP_MAX_SECONDS:
            check.update(rms=None, decision="too_long")
        else:
            interval = audio[max(0, round(start * sample_rate)):min(len(audio), round(end * sample_rate))]
            rms = math.sqrt(float(np.mean(interval.astype(np.float64) ** 2))) if len(interval) else 0.0
            check.update(rms=round(rms, 5),
                         decision="candidate" if len(interval) >= sample_rate * GAP_MIN_SECONDS
                         and rms >= GAP_MIN_RMS else "quiet")
        checks.append(check)
    return checks


def gap_candidates(segments: list[dict], audio: "np.ndarray", sample_rate: int) -> list[tuple[float, float]]:
    """Find sustained, nonquiet audio between adjacent ASR segments."""
    return [(check["start"], check["end"]) for check in _gap_scan(segments, audio, sample_rate)
            if check["decision"] == "candidate"]


def write_transcript_sidecars(transcript_path: Path, segments: list[dict]) -> None:
    """Keep WhisperX text exports in sync when a JSON transcript is repaired."""
    def stamp(seconds: float, separator: str) -> str:
        milliseconds = round(float(seconds) * 1000)
        hours, remainder = divmod(milliseconds, 3600000)
        minutes, remainder = divmod(remainder, 60000)
        whole_seconds, fraction = divmod(remainder, 1000)
        return f"{hours:02}:{minutes:02}:{whole_seconds:02}{separator}{fraction:03}"

    lines = []
    vtt = ["WEBVTT", ""]
    tsv = ["start\tend\ttext"]
    plain = []
    for index, segment in enumerate(segments, 1):
        text = str(segment["text"]).strip()
        start, end = float(segment["start"]), float(segment["end"])
        lines.append(f"{index}\n{stamp(start, ',')} --> {stamp(end, ',')}\n{text}\n")
        vtt.append(f"{stamp(start, '.')} --> {stamp(end, '.')}\n{text}\n")
        tsv.append(f"{round(start * 1000)}\t{round(end * 1000)}\t{text}")
        plain.append(text)
    for suffix, content in ((".srt", "\n".join(lines) + "\n"),
                            (".vtt", "\n".join(vtt) + "\n"),
                            (".tsv", "\n".join(tsv) + "\n"),
                            (".txt", "\n".join(plain) + "\n")):
        transcript_path.with_suffix(suffix).write_text(content, encoding="utf-8")


def recover_transcript_gaps(audio_path: Path, transcript_path: Path,
                            logger: Callable[[str], None] | None = None) -> int:
    """Second-pass recognition without VAD for likely omissions in WhisperX output."""
    import numpy as np
    from faster_whisper import WhisperModel

    data = json.loads(transcript_path.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    diagnostic_path = transcript_path.with_suffix(".diagnostics.json")

    def emit(message: str) -> None:
        if logger:
            logger(f"ASR 复查：{message}")

    if data.get("gap_recovery_checked"):
        emit(f"转写已检查，已有补回片段 {data.get('gap_recovery_count', 0)}；详情 {diagnostic_path}")
        return 0
    if len(segments) < 2:
        emit(f"原始片段仅 {len(segments)} 条，无法比较片段间空白")
        return 0
    with wave.open(str(audio_path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("Gap recovery requires mono 16-bit PCM WAV")
        sample_rate = source.getframerate()
        audio = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768
    checks = _gap_scan(segments, audio, sample_rate)
    candidates = [check for check in checks if check["decision"] == "candidate"]
    diagnostics = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "audio_file": audio_path.name, "transcript_file": transcript_path.name,
        "audio_duration": round(len(audio) / sample_rate, 3), "sample_rate": sample_rate,
        "original_segment_count": len(segments),
        "detection": {"min_gap_seconds": GAP_MIN_SECONDS, "max_gap_seconds": GAP_MAX_SECONDS,
                      "min_rms": GAP_MIN_RMS, "model": "faster-whisper-small",
                      "device": "cpu", "compute_type": "int8", "vad_filter": False},
        "gaps": checks, "recovered_segment_count": 0,
        "final_segment_count": len(segments),
    }

    def save_diagnostics() -> None:
        diagnostic_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")

    emit(f"音频 {audio_path.name}，时长 {diagnostics['audio_duration']:.2f}s，原始片段 {len(segments)}；"
         f"≥{GAP_MIN_SECONDS:g}s 空白 {len(checks)} 处，待补识别 {len(candidates)} 处")
    for check in checks:
        emit(f"空白 {check['start']:.2f}–{check['end']:.2f}s，{check['duration']:.2f}s，"
             f"RMS={check['rms'] if check['rms'] is not None else '未测'}，判定={check['decision']}")
    if not candidates:
        save_diagnostics()
        emit(f"没有需要补识别的区间；详情 {diagnostic_path}")
        return 0
    # CPU inference is dependable on Windows even if the GPU runtime lacks a DLL.
    try:
        model = WhisperModel("small", device="cpu", compute_type="int8",
                             download_root=str(PROJECT_ROOT / "models" / "whisper"),
                             local_files_only=True)
    except Exception as exc:
        diagnostics["error"] = f"model_load: {type(exc).__name__}: {exc}"
        save_diagnostics()
        emit(f"模型加载失败：{exc}；详情 {diagnostic_path}")
        raise
    recovered = []
    misplaced = set()
    for check in candidates:
        start, end = check["start"], check["end"]
        check["recognition"] = []
        local_recovered = []
        clip_start = max(0.0, start - 2.0)
        clip_end = min(len(audio) / sample_rate, end + 2.0)
        check["clip_start"], check["clip_end"] = round(clip_start, 3), round(clip_end, 3)
        clip = audio[round(clip_start * sample_rate):round(clip_end * sample_rate)]
        try:
            results, _ = model.transcribe(clip, language="en", vad_filter=False, beam_size=5,
                                          condition_on_previous_text=False, word_timestamps=True)
            results = list(results)
        except Exception as exc:
            check["error"] = f"{type(exc).__name__}: {exc}"
            save_diagnostics()
            emit(f"区间 {start:.2f}–{end:.2f}s 补识别失败：{exc}；详情 {diagnostic_path}")
            raise
        previous = next((item for item in segments if abs(float(item["end"]) - start) < .002), None)
        following = next((item for item in segments if abs(float(item["start"]) - end) < .002), None)
        normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
        full_text = normalize(" ".join(item.text for item in results))
        misplaced_previous = bool(previous and float(previous["start"]) >= clip_start + .05
                                  and float(previous["end"]) - float(previous["start"]) < 3.5
                                  and len(normalize(previous["text"])) >= 15
                                  and normalize(previous["text"]) in full_text)
        recovery_start = float(previous["start"]) if misplaced_previous else start
        for result in results:
            record = {"start": round(clip_start + result.start, 3),
                      "end": round(clip_start + result.end, 3),
                      "text": result.text.strip(),
                      "avg_logprob": round(result.avg_logprob, 3),
                      "no_speech_prob": round(result.no_speech_prob, 3)}
            check["recognition"].append(record)
            words = [{"word": word.word.strip(), "start": round(clip_start + word.start, 3),
                      "end": round(clip_start + word.end, 3)}
                     for word in (result.words or []) if word.word.strip()
                     and not re.search(r"(.)\1{9,}", word.word)]
            words = [word for word in words if word["end"] > word["start"]
                     and recovery_start - .05 <= (word["start"] + word["end"]) / 2 <= end + .05]
            text = " ".join(word["word"] for word in words)
            if (len(words) < 2 or len(text) < 8 or result.avg_logprob < -1.0
                    or result.no_speech_prob > .6):
                record["decision"] = "rejected"
                record["reason"] = ("too_few_words" if len(words) < 2 or len(text) < 8 else
                                    "low_logprob" if result.avg_logprob < -1.0 else "no_speech")
                continue
            record["decision"] = "accepted"
            record["kept_text"] = text
            local_recovered.append({"start": max(recovery_start, words[0]["start"]),
                                    "end": min(end, words[-1]["end"]), "text": text, "words": words})
        if local_recovered:
            if misplaced_previous:
                misplaced.add(id(previous))
                check["replaced_previous_text"] = previous["text"].strip()
            if following:
                final = local_recovered[-1]
                next_words = [normalize(word) for word in following["text"].split()]
                for overlap in range(min(len(final["words"]), len(next_words), 8), 0, -1):
                    if [normalize(word["word"]) for word in final["words"][-overlap:]] == next_words[:overlap]:
                        del final["words"][-overlap:]
                        if final["words"]:
                            final["end"] = final["words"][-1]["end"]
                            final["text"] = " ".join(word["word"] for word in final["words"])
                        else:
                            local_recovered.pop()
                        check["trimmed_following_overlap_words"] = overlap
                        break
            if len(local_recovered) >= 2 and len(local_recovered[-1]["words"]) < 3:
                tail = local_recovered.pop()
                local_recovered[-1]["words"].extend(tail["words"])
                local_recovered[-1]["end"] = tail["end"]
                local_recovered[-1]["text"] = " ".join(word["word"] for word in local_recovered[-1]["words"])
            recovered.extend(local_recovered)
        check["kept_segments"] = [{"start": item["start"], "end": item["end"],
                                   "text": item["text"]} for item in local_recovered]
        emit(f"区间 {start:.2f}–{end:.2f}s：模型返回 {len(results)} 段，保留 {len(local_recovered)} 段"
             + ("，替换前一段错位文本" if misplaced_previous else "")
             + (f"，去除后段重叠 {check['trimmed_following_overlap_words']} 词"
                if check.get("trimmed_following_overlap_words") else ""))
    if recovered:
        raw_path = transcript_path.with_suffix(".raw.json")
        raw_path.write_bytes(transcript_path.read_bytes())
        diagnostics["raw_transcript_file"] = raw_path.name
        data["segments"] = sorted([item for item in segments if id(item) not in misplaced] + recovered,
                                  key=lambda item: float(item["start"]))
    data["gap_recovery_count"] = len(recovered)
    data["gap_recovery_checked"] = True
    transcript_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if recovered:
        write_transcript_sidecars(transcript_path, data["segments"])
    diagnostics["recovered_segment_count"] = len(recovered)
    diagnostics["final_segment_count"] = len(data["segments"])
    save_diagnostics()
    emit(f"完成，补回 {len(recovered)} 段，最终 {len(data['segments'])} 段；详情 {diagnostic_path}"
         + (f"；原始转写 {diagnostics['raw_transcript_file']}" if recovered else ""))
    return len(recovered)


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
    print(f"ASR 开始：source={selected_source}，audio={audio_path.name}，model={args.model}，"
          f"batch_size={args.batch_size}，vad=silero，chunk_size=15，device=cuda，compute=float16",
          flush=True)
    result = subprocess.run(command, env=environment, check=False)
    if result.returncode:
        print(f"ASR 失败：WhisperX 退出码 {result.returncode}", flush=True)
        return result.returncode
    transcript = job_dir / ("transcript" if args.audio_source == "auto" else f"transcript_{selected_source}") / f"{audio_path.stem}.json"
    if transcript.is_file():
        recover_transcript_gaps(audio_path, transcript, logger=lambda message: print(message, flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
