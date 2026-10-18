"""Bounded-rate Azure Speech synthesis and timeline assembly for one-speaker lessons."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import statistics
import subprocess
import uuid
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from ..rendering.watermark import drawtext_filter
from ..rendering.subtitles import subtitle_filter
from xml.sax.saxutils import escape

import keyring
import numpy as np
import requests

SERVICE = "VidLingua Azure Speech"
ACCOUNT = "local-speech-key"
POOL_ACCOUNT = "speech-key-pool"
VOICES = {
    "zh-CN-YunyangNeural": "云扬 · 男声",
    "zh-CN-XiaoxiaoNeural": "晓晓 · 女声",
    "zh-CN-YunjianNeural": "云健 · 男声",
    "zh-CN-YunxiNeural": "云希 · 男声",
    "zh-CN-XiaoyiNeural": "晓伊 · 女声",
    "zh-CN-XiaochenNeural": "晓辰 · 女声",
    "zh-CN-XiaohanNeural": "晓涵 · 女声",
    "zh-CN-YunyeNeural": "云野 · 男声",
}
DEFAULTS = {"region": "eastasia", "voice": "zh-CN-YunyangNeural",
            "hd_temperature": 0.4,
            "max_synthesis_adjust": 0.10, "max_post_adjust": 0.15,
            "max_global_adjust": 0.08, "max_local_adjust": 0.05,
            "max_video_adjust": 0.18,
            "per_key_concurrency": 1,
            "background_volume": 0.55}
# Slow one spoken shot, and speed up only the pause after it, by at most this fraction.
MAX_VIDEO_ADJUST = 0.15
SAMPLE_RATE = 24000
SYNTHESIS_MAX_ATTEMPTS = 5
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


def load_config(path: Path) -> dict:
    saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {**DEFAULTS, **{key: value for key, value in saved.items() if key in DEFAULTS}}


def validate_config(config: dict) -> dict:
    region = config["region"].strip().lower()
    if region and not re.fullmatch(r"[a-z0-9-]{2,40}", region):
        raise ValueError("Azure 区域格式不正确，例如 eastasia 或 southeastasia")
    if not valid_voice_name(config["voice"]):
        raise ValueError("不支持的 Azure 中文音色")
    for name, low, high in (("max_synthesis_adjust", 0, .1),
                            ("hd_temperature", .3, 1),
                            ("max_post_adjust", 0, .75), ("max_global_adjust", 0, .3),
                            ("max_local_adjust", 0, .25), ("max_video_adjust", 0, .5),
                            ("background_volume", 0, 1)):
        if not low <= float(config[name]) <= high:
            raise ValueError(f"{name} 必须在 {low}–{high} 之间")
    concurrency = config["per_key_concurrency"]
    if isinstance(concurrency, bool) or int(concurrency) != concurrency or not 1 <= concurrency <= 8:
        raise ValueError("每把密钥并发数必须在 1–8 之间")
    return {**config, "region": region}


def valid_voice_name(voice: str) -> bool:
    return isinstance(voice, str) and bool(re.fullmatch(
        r"zh-CN-[A-Za-z0-9]+(?:Neural|:DragonHD(?:Omni|Flash)?LatestNeural)", voice))


def is_hd_voice(voice: str) -> bool:
    return ":DragonHD" in voice


def chinese_voices(voices: list[dict]) -> list[dict]:
    result = []
    for item in voices:
        name = item.get("ShortName", "")
        if item.get("Locale") != "zh-CN" or not valid_voice_name(name):
            continue
        result.append({"name": name, "local_name": item.get("LocalName") or item.get("DisplayName") or name,
                       "gender": item.get("Gender", ""), "hd": is_hd_voice(name)})
    return sorted(result, key=lambda item: (not item["hd"], item["name"]))


def save_config(path: Path, config: dict) -> None:
    config = validate_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_key() -> str | None:
    keys = get_keys()
    return keys[0] if keys else None


def get_keys() -> list[str]:
    stored = keyring.get_password(SERVICE, POOL_ACCOUNT)
    if stored:
        return [validate_key(key) for key in json.loads(stored)]
    legacy = keyring.get_password(SERVICE, ACCOUNT) or os.environ.get("AZURE_SPEECH_KEY")
    return [validate_key(legacy)] if legacy else []


def set_keys(keys: list[str]) -> None:
    validated = list(dict.fromkeys(validate_key(key) for key in keys if key.strip()))
    if not validated or len(validated) > 16:
        raise ValueError("请填写 1–16 把 Azure Speech 密钥")
    keyring.set_password(SERVICE, POOL_ACCOUNT, json.dumps(validated))


def validate_key(key: str) -> str:
    key = key.strip()
    if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("Azure Speech 密钥格式不正确；请只粘贴资源密钥")
    return key


def set_key(key: str) -> None:
    keyring.set_password(SERVICE, ACCOUNT, validate_key(key))


def delete_key() -> None:
    for account in (POOL_ACCOUNT, ACCOUNT):
        try:
            keyring.delete_password(SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            pass


def voice_list(key: str, region: str) -> list[dict]:
    response = requests.get(f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list",
                            headers={"Ocp-Apim-Subscription-Key": validate_key(key)}, timeout=(10, 30))
    if not response.ok:
        raise RuntimeError(f"Azure Speech 连接失败：HTTP {response.status_code}")
    return response.json()


def pronunciation_content(text: str, annotations: list[dict] | None,
                          use_alias: bool) -> str:
    valid = {}
    for item in annotations or []:
        target, pinyin, alias = item.get("text"), item.get("pinyin"), item.get("alias")
        if not all(isinstance(value, str) for value in (target, pinyin, alias)):
            continue
        if target not in text or not re.fullmatch(r"[\u4e00-\u9fff]{1,8}", target):
            continue
        if not re.fullmatch(r"[a-z]+[1-5](?:\s+[a-z]+[1-5])*", pinyin):
            continue
        if len(pinyin.split()) != len(target) or not re.fullmatch(r"[\u4e00-\u9fff]{1,8}", alias):
            continue
        valid.setdefault(target, item)
    if not valid:
        return escape(text)
    pattern = re.compile("|".join(re.escape(target) for target in sorted(valid, key=len, reverse=True)))
    parts, offset = [], 0
    for match in pattern.finditer(text):
        parts.append(escape(text[offset:match.start()]))
        item = valid[match.group()]
        if use_alias:
            alias = escape(item["alias"], {"'": "&apos;", '"': "&quot;"})
            parts.append(f"<sub alias='{alias}'>{escape(match.group())}</sub>")
        else:
            phonemes = " ".join(re.sub(r"^([a-z]+)([1-5])$", r"\1 \2", syllable)
                                for syllable in item["pinyin"].split())
            parts.append(f"<phoneme alphabet='sapi' ph='{phonemes}'>{escape(match.group())}</phoneme>")
        offset = match.end()
    parts.append(escape(text[offset:]))
    return "".join(parts)


def ssml(text: str, voice: str, rate: float, annotations: list[dict] | None = None,
         use_alias: bool = False, hd_temperature: float | None = None) -> str:
    if not valid_voice_name(voice):
        raise ValueError("不支持的 Azure 中文音色")
    alias = use_alias or ":DragonHDOmni" in voice or ":DragonHDFlash" in voice
    content = pronunciation_content(text, annotations, alias)
    if not is_hd_voice(voice):
        content = f"<prosody rate='{rate:.3f}'>{content}</prosody>"
    parameters = (f" parameters='temperature={float(hd_temperature):.2f}'"
                  if is_hd_voice(voice) and hd_temperature is not None else "")
    return ("<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
            f"<voice name='{voice}'{parameters}>{content}"
            "</voice></speak>")


def synthesize(key: str, config: dict, text: str, rate: float,
               annotations: list[dict] | None = None) -> bytes:
    url = f"https://{config['region']}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {"Ocp-Apim-Subscription-Key": validate_key(key),
               "Content-Type": "application/ssml+xml; charset=utf-8",
               "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm", "User-Agent": "VidLingua"}
    response = _speech_post_with_retry(
        url, headers, ssml(text, config["voice"], rate, annotations,
                           hd_temperature=config.get("hd_temperature", DEFAULTS["hd_temperature"])).encode("utf-8"))
    if (response.status_code == 400 and annotations
            and ":DragonHDOmni" not in config["voice"] and ":DragonHDFlash" not in config["voice"]):
        response = _speech_post_with_retry(
            url, headers, ssml(text, config["voice"], rate, annotations, use_alias=True,
                               hd_temperature=config.get("hd_temperature", DEFAULTS["hd_temperature"])).encode("utf-8"))
    if not response.ok:
        raise RuntimeError(f"Azure Speech 合成失败：HTTP {response.status_code}")
    samples = read_wav(response.content)
    if not len(samples):
        raise RuntimeError("Azure Speech 返回了空音频")
    return response.content


def _speech_post_with_retry(url: str, headers: dict, body: bytes):
    """Retry only transient Azure/network failures; leave invalid SSML and auth errors visible."""
    for attempt in range(SYNTHESIS_MAX_ATTEMPTS):
        try:
            response = requests.post(url, headers=headers, data=body, timeout=(10, 120))
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as exc:
            if attempt == SYNTHESIS_MAX_ATTEMPTS - 1:
                raise RuntimeError(f"Azure Speech 连接失败，已尝试 {SYNTHESIS_MAX_ATTEMPTS} 次：{exc}") from exc
            time.sleep(min(30, 2 ** attempt))
            continue
        if response.status_code not in RETRYABLE_HTTP_STATUS:
            return response
        if attempt == SYNTHESIS_MAX_ATTEMPTS - 1:
            raise RuntimeError(f"Azure Speech 合成失败：HTTP {response.status_code}，已尝试 {SYNTHESIS_MAX_ATTEMPTS} 次")
        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = 2 ** attempt
        time.sleep(min(30, max(0.1, delay)))
    raise AssertionError("unreachable")


def read_wav(data: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(data), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise RuntimeError("配音 WAV 格式与请求不一致")
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").copy()


def write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.astype("<i2", copy=False).tobytes())


def segment_window(segments: list[dict], index: int, video_duration: float) -> float:
    segment = segments[index]
    start, end = float(segment["start"]), float(segment["end"])
    next_start = float(segment.get("_next_start", segments[index + 1]["start"] if index + 1 < len(segments) else video_duration))
    window_end = min(video_duration, end + min(.35, max(0, next_start - end - .08)))
    return max(.2, window_end - start)


def choose_global_baseline(durations: list[float], windows: list[float], config: dict,
                           limit: float | None = None) -> dict:
    """Choose a robust shared speed from measured audio, before fitting individual clips."""
    ratios = sorted(duration / window for duration, window in zip(durations, windows)
                    if duration >= .4 and window >= .8)
    if not ratios:
        ratios = sorted(duration / window for duration, window in zip(durations, windows) if window > 0)
    measured = statistics.median(ratios) if ratios else 1.0
    position = .75 * (len(ratios) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ratios) - 1)
    target = ratios[lower] + (ratios[upper] - ratios[lower]) * (position - lower) if ratios else 1.0
    limit = limit if limit is not None else 1 + float(config["max_global_adjust"])
    speed = round(min(limit, max(1.0, target)), 3)
    return {"speed": speed, "measured_median_ratio": round(measured, 3),
            "measured_p75_ratio": round(target, 3), "measured_segments": len(ratios),
            "method": "p75_natural_duration_to_window"}


def clip_cache_key(segment: dict, config: dict, rate: float) -> str:
    value = [segment["id"], segment["zh"], config["region"], config["voice"], rate]
    if (config.get("engine") or "azure") != "azure":
        value.append(config["engine"])
    if is_hd_voice(config["voice"]):
        value.append(config.get("hd_temperature", DEFAULTS["hd_temperature"]))
    if segment.get("pronunciations"):
        value.append(segment["pronunciations"])
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def stretch(samples: np.ndarray, speed: float) -> np.ndarray:
    """Change duration without dropping the final syllable.

    FFmpeg atempo keeps an overlap window and can discard the tail of a short clip.
    Silence is appended first so that window falls on the padding, then most of the
    padding is removed and a short tail is kept.
    """
    if abs(speed - 1) < .001 or not len(samples):
        return samples
    pad = int(.25 * SAMPLE_RATE)
    padded = np.concatenate([samples, np.zeros(pad, dtype=np.int16)])
    source = io.BytesIO()
    with wave.open(source, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(SAMPLE_RATE)
        wav.writeframes(padded.astype("<i2", copy=False).tobytes())
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                             "-f", "wav", "-i", "pipe:0", "-af", f"atempo={speed:.5f}",
                             "-f", "wav", "pipe:1"], input=source.getvalue(),
                            capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("FFmpeg 音频时间伸缩失败")
    stretched = read_wav(result.stdout)
    keep = max(1, int(round(len(samples) / speed)))
    if len(stretched) <= keep:
        return stretched
    # Keep a shifted final syllable, but do not retain the protective silence.
    search_end = min(len(stretched), keep + int(.08 * SAMPLE_RATE))
    loud = np.flatnonzero(np.abs(stretched[:search_end]) > 200)
    end = keep
    if len(loud) and int(loud[-1]) >= keep:
        end = min(search_end, int(loud[-1]) + int(.01 * SAMPLE_RATE))
    return stretched[:end]


def _mix_into(track: np.ndarray, offset: int, samples: np.ndarray) -> np.ndarray:
    """Add a clip without discarding samples that run past the current buffer."""
    if offset < 0:
        samples = samples[-offset:]
        offset = 0
    if not len(samples):
        return track
    end = offset + len(samples)
    if end > len(track):
        grown = np.zeros(end, dtype=np.int32)
        grown[:len(track)] = track
        track = grown
    track[offset:end] += samples.astype(np.int32, copy=False)
    return track


def _ensure_clips(segments: list[dict], config: dict, keys: list[str], cache_dir: Path,
                  rate: float, progress=None, synthesize_fn=None) -> None:
    missing = [(segment, cache_dir / f"{segment['id']}-{clip_cache_key(segment, config, rate)}.wav")
               for segment in segments]
    pending = [(segment, path) for segment, path in missing if not path.is_file()]
    completed = len(missing) - len(pending)
    if progress:
        progress(completed)
    if not pending:
        return
    synth = synthesize_fn or synthesize
    executors = [ThreadPoolExecutor(max_workers=int(config["per_key_concurrency"])) for _ in keys]
    futures = {}
    failures = []
    try:
        for index, (segment, path) in enumerate(pending):
            key_index = index % len(keys)
            args = (keys[key_index], config, segment["zh"], rate)
            annotations = segment.get("pronunciations") or []
            future = executors[key_index].submit(synth, *args, annotations) if annotations else executors[key_index].submit(synth, *args)
            futures[future] = (segment["id"], path)
        for future in as_completed(futures):
            segment_id, path = futures[future]
            try:
                audio = future.result()
            except Exception as exc:
                failures.append((segment_id, exc))
                continue
            path.write_bytes(audio)
            completed += 1
            if progress:
                progress(completed)
    finally:
        for executor in executors:
            executor.shutdown(wait=True, cancel_futures=True)
    if failures:
        segment_id, error = failures[0]
        engine_name = "豆包" if config.get("engine") == "doubao" else "Azure"
        raise RuntimeError(f"{len(failures)} 句 {engine_name} 合成失败（首个句段 {segment_id}）：{error}") from error


def make_voice_track(segments: list[dict], config: dict, key: str | list[str], cache_dir: Path,
                     video_duration: float, progress=None,
                     baseline_info: dict | None = None,
                     synthesize_fn=None) -> tuple[np.ndarray, list[dict], list[dict]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    track = np.zeros(int((video_duration + 1) * SAMPLE_RATE), dtype=np.int32)
    issues = []
    timings = []
    # Standard voices use a small, distributed pilot to choose one Azure rate.
    # HD cannot use SSML prosody, so its shared baseline is applied after synthesis.
    hd = is_hd_voice(config["voice"])
    keys = [key] if isinstance(key, str) else key
    if not keys:
        raise ValueError("尚未配置 Azure Speech 密钥")
    rate = 1.0
    azure_factor = 1.0
    pilot_info = None
    if not hd and len(segments) >= 8 and float(config["max_synthesis_adjust"]) > 0:
        eligible = [i for i in range(len(segments)) if segment_window(segments, i, video_duration) >= .8]
        if len(eligible) >= 4:
            count = min(12, max(4, len(eligible) // 6))
            indices = sorted({eligible[round(i * (len(eligible) - 1) / (count - 1))]
                              for i in range(count)})
            _ensure_clips([segments[index] for index in indices], config, keys, cache_dir, rate,
                          synthesize_fn=synthesize_fn)
            pilot_durations, pilot_windows = [], []
            for index in indices:
                segment = segments[index]
                clip_path = cache_dir / f"{segment['id']}-{clip_cache_key(segment, config, rate)}.wav"
                pilot_durations.append(len(read_wav(clip_path.read_bytes())) / SAMPLE_RATE)
                pilot_windows.append(segment_window(segments, index, video_duration))
            pilot_info = choose_global_baseline(pilot_durations, pilot_windows, config)
            azure_factor = min(pilot_info["speed"], 1 + float(config["max_synthesis_adjust"]))
            if azure_factor >= 1.015:
                rate = round(rate * azure_factor, 3)
            else:
                azure_factor = 1.0
    windows = []
    natural_durations = []
    clips = []
    _ensure_clips(segments, config, keys, cache_dir, rate, progress, synthesize_fn)
    for index, segment in enumerate(segments):
        window = segment_window(segments, index, video_duration)
        clip_path = cache_dir / f"{segment['id']}-{clip_cache_key(segment, config, rate)}.wav"
        samples = read_wav(clip_path.read_bytes())
        clips.append(clip_path)
        windows.append(window)
        natural_durations.append(len(samples) / SAMPLE_RATE)
    remaining_limit = (1 + float(config["max_global_adjust"])) / azure_factor
    baseline = choose_global_baseline(natural_durations, windows, config,
                                      limit=min(max(1.0, remaining_limit),
                                                1 + float(config["max_post_adjust"])))
    baseline["azure_rate"] = rate
    baseline["azure_factor"] = round(azure_factor, 3)
    baseline["combined_speed"] = round(rate * baseline["speed"], 3)
    if pilot_info:
        baseline["pilot"] = pilot_info
    if baseline_info is not None:
        baseline_info.update(baseline)
    for index, segment in enumerate(segments):
        start = float(segment["start"])
        next_start = float(segment.get("_next_start", segments[index + 1]["start"] if index + 1 < len(segments) else video_duration))
        window = windows[index]
        samples = read_wav(clips[index].read_bytes())
        natural_duration = natural_durations[index]
        required_speed = natural_duration / window
        local_ceiling = min(1 + float(config["max_post_adjust"]),
                            baseline["speed"] * (1 + float(config["max_local_adjust"])))
        post_speed = round(min(local_ceiling, max(baseline["speed"], required_speed)), 4)
        samples = stretch(samples, post_speed)
        actual_duration = len(samples) / SAMPLE_RATE
        overrun = max(0., actual_duration - window)
        timings.append({"id": segment["id"], "start": start, "window_seconds": round(window, 3),
                        "azure_rate": rate, "natural_seconds": round(natural_duration, 3),
                        "required_speed": round(required_speed, 3),
                        "global_speed": baseline["speed"], "post_speed": post_speed,
                        "audio_seconds": round(actual_duration, 6), "overrun_seconds": round(overrun, 3)})
        if overrun > .03:
            issues.append({"id": segment["id"], "kind": "overrun", "seconds": round(overrun, 3),
                           "window_seconds": round(window, 3), "audio_seconds": round(actual_duration, 3),
                           "suggestion": "缩短中文台词；若随后停顿足够，配音时会小幅放慢该句画面"})
        overlap = start + actual_duration - next_start
        if index + 1 < len(segments) and overlap > .03:
            issues.append({"id": segment["id"], "kind": "overlap", "seconds": round(overlap, 3),
                           "suggestion": "本句配音与下一句重叠，请缩短中文台词"})
        track = _mix_into(track, round(start * SAMPLE_RATE), samples)
    if np.max(np.abs(track), initial=0) > 32767:
        issues.append({"kind": "overlap", "suggestion": "配音片段发生重叠，混音前需处理"})
    return np.clip(track, -32768, 32767).astype("<i2"), issues, timings


def apply_video_fit(segments: list[dict], timings: list[dict], issues: list[dict],
                    video_duration: float, max_adjust: float = MAX_VIDEO_ADJUST) -> tuple[list[dict], list[dict]]:
    """Slow a spoken shot just enough to cover its audio, and take that time from the following pause.

    Later sentences stay on their original timestamps. A shot is left unchanged when the needed
    slowdown or pause speedup would exceed max_adjust, or when the voice already overlaps the next line.
    """
    overlap_ids = {item.get("id") for item in issues if item.get("kind") == "overlap"}
    timing_by_id = {item["id"]: item for item in timings}
    ordered = list(segments)
    index_by_id = {item["id"]: index for index, item in enumerate(ordered)}
    retiming = []
    blocked = []
    for issue in issues:
        if issue.get("kind") != "overrun" or issue.get("id") in overlap_ids:
            if issue.get("kind") == "overrun" and issue.get("id") in overlap_ids:
                blocked.append({**issue, "suggestion": "配音已与下一句重叠；局部画面调速不能挪动下一句，请缩短本句中文台词"})
            else:
                blocked.append(issue)
            continue
        segment = next((item for item in ordered if item["id"] == issue.get("id")), None)
        timing = timing_by_id.get(issue.get("id"))
        if segment is None or timing is None:
            blocked.append(issue)
            continue
        extra = float(issue["seconds"])
        start = float(segment["start"])
        end = float(segment["end"])
        speech = end - start
        index = index_by_id[segment["id"]]
        next_start = float(ordered[index + 1]["start"]) if index + 1 < len(ordered) else float(video_duration)
        pause = next_start - end
        video_speed = speech / (speech + extra) if speech + extra > 0 else 0.0
        pause_speed = pause / (pause - extra) if pause > extra else 99.0
        within_limit = (speech > .05 and video_speed >= 1 - max_adjust - 1e-6
                        and pause_speed <= 1 + max_adjust + 1e-6)
        if not within_limit:
            if pause <= extra:
                reason = "后续停顿不足以归还所需时长"
            elif video_speed < 1 - max_adjust - 1e-6:
                reason = f"画面需放慢至 {video_speed:.2f} 倍，超出 {max_adjust:.0%} 上限"
            else:
                reason = f"后续停顿需加快至 {pause_speed:.2f} 倍，超出 {max_adjust:.0%} 上限"
            blocked.append({**issue, "suggestion": f"{reason}；请缩短本句中文台词"})
            continue
        retiming.append({
            "id": segment["id"], "start": round(start, 3), "end": round(end, 3),
            "next_start": round(next_start, 3), "video_speed": round(video_speed, 3),
            "pause_speed": round(pause_speed, 3), "extra_seconds": round(extra, 3),
            "subtitle_end": round(start + float(timing["audio_seconds"]), 3),
            "note": "放慢该句画面，并加快随后停顿，使后面的句段保持原时间",
        })
    return retiming, blocked


def retime_pieces(retiming: list[dict], duration: float) -> list[dict]:
    pieces = []
    cursor = 0.0
    for item in retiming:
        if item["start"] > cursor + 1e-3:
            pieces.append({"start": cursor, "end": item["start"], "speed": 1.0})
        pieces.append({"start": item["start"], "end": item["end"], "speed": item["video_speed"]})
        if item["next_start"] > item["end"] + 1e-3:
            pieces.append({"start": item["end"], "end": item["next_start"], "speed": item["pause_speed"]})
        cursor = item["next_start"]
    if duration > cursor + 1e-3:
        pieces.append({"start": cursor, "end": duration, "speed": 1.0})
    return pieces


def retimed_segments(segments: list[dict], retiming: list[dict]) -> list[dict]:
    by_id = {item["id"]: item for item in retiming}
    result = []
    for segment in segments:
        item = dict(segment)
        fix = by_id.get(segment["id"])
        if fix:
            item["end"] = fix["subtitle_end"]
        result.append(item)
    return result


def plan_elastic_timeline(segments: list[dict], timings: list[dict], video_duration: float,
                          max_adjust: float = MAX_VIDEO_ADJUST,
                          min_gap: float = .05) -> tuple[list[dict], list[dict], list[dict], list[dict], float]:
    """Fit each source interval to its dubbed audio and carry that shift through the rest of the film.

    A longer clip slows the picture from this line to the next, and a shorter clip speeds
    that same interval up. Both stay inside max_adjust. Later cues move with the new timeline.
    """
    by_id = {item["id"]: item for item in timings}
    pieces = []
    cues = []
    adjustments = []
    issues = []
    cursor = 0.0
    output_cursor = 0.0

    def add_piece(start: float, end: float, speed: float) -> None:
        if end - start <= 1e-6:
            return
        if pieces and abs(pieces[-1]["end"] - start) < 1e-6 and abs(pieces[-1]["speed"] - speed) < 1e-6:
            pieces[-1]["end"] = end
        else:
            pieces.append({"start": start, "end": end, "speed": speed})

    for index, segment in enumerate(segments):
        start = float(segment["start"])
        next_start = float(segment.get("_next_start", segments[index + 1]["start"] if index + 1 < len(segments) else video_duration))
        if start < cursor - 1e-5 or next_start <= start:
            raise ValueError(f"句段 {segment['id']} 的时间顺序无效")
        if start > cursor:
            add_piece(cursor, start, 1.0)
            output_cursor += start - cursor
        source_span = next_start - start
        audio_seconds = float(by_id[segment["id"]]["audio_seconds"])
        gap = min_gap if next_start < video_duration else 0.0
        desired_span = max(1e-3, audio_seconds + gap)
        desired_speed = source_span / desired_span
        speed = round(min(1 + max_adjust, max(1 - max_adjust, desired_speed)), 6)
        actual_span = source_span / speed
        output_start = output_cursor
        output_end = output_start + audio_seconds
        if output_end + gap > output_start + actual_span + .005:
            issues.append({"id": segment["id"], "kind": "timeline_limit",
                           "seconds": round(output_end + gap - output_start - actual_span, 3),
                           "required_video_speed": round(desired_speed, 3),
                           "suggestion": (f"这一段需放慢到 {desired_speed:.3f} 倍，超出画面调速上限 "
                                          f"{max_adjust:.0%}；请精简中文台词或提高画面调速上限")})
        add_piece(start, next_start, speed)
        cue = dict(segment)
        cue.pop("_next_start", None)
        cue["start"] = round(output_start, 3)
        cue["end"] = round(output_end, 3)
        cues.append(cue)
        by_id[segment["id"]]["output_start"] = cue["start"]
        by_id[segment["id"]]["output_end"] = cue["end"]
        if speed < .999 or speed > 1.001:
            faster = speed > 1
            adjustments.append({"id": segment["id"], "source_start": round(start, 3),
                                "source_end": round(next_start, 3),
                                "video_speed": speed,
                                "added_seconds": round(actual_span - source_span, 3),
                                "output_start": cue["start"],
                                "note": ("加快当前句到下句之间的画面与背景音，后续配音和字幕同步提前"
                                         if faster else
                                         "放慢当前句到下句之间的画面与背景音，后续配音和字幕同步顺延")})
        output_cursor += actual_span
        cursor = next_start
    if video_duration > cursor:
        add_piece(cursor, video_duration, 1.0)
        output_cursor += video_duration - cursor
    return pieces, cues, adjustments, issues, output_cursor


def assemble_elastic_voice_track(segments: list[dict], timings: list[dict], config: dict,
                                 cache_dir: Path, output_duration: float) -> np.ndarray:
    """Place each fitted clip at its retimed start instead of mixing on the source timeline."""
    by_id = {item["id"]: item for item in timings}
    track = np.zeros(int((output_duration + 1) * SAMPLE_RATE), dtype=np.int32)
    for segment in segments:
        timing = by_id[segment["id"]]
        rate = float(timing["azure_rate"])
        clip_path = cache_dir / f"{segment['id']}-{clip_cache_key(segment, config, rate)}.wav"
        samples = stretch(read_wav(clip_path.read_bytes()), float(timing["post_speed"]))
        track = _mix_into(track, round(float(timing["output_start"]) * SAMPLE_RATE), samples)
    if np.max(np.abs(track), initial=0) > 32767:
        raise RuntimeError("顺延后的配音轨仍存在重叠，请检查时间轴")
    return np.clip(track, -32768, 32767).astype("<i2")


def _retime_filter(pieces: list[dict], background_volume: float,
                   video_filter: str | None = None, keep_background: bool = True) -> str:
    video_parts = []
    audio_parts = []
    video_labels = []
    audio_labels = []
    for index, piece in enumerate(pieces):
        start, end, speed = float(piece["start"]), float(piece["end"]), float(piece["speed"])
        video_parts.append(
            f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=(PTS-STARTPTS)/{speed:.6f}[v{index}]")
        if keep_background:
            audio = f"[1:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS"
            if abs(speed - 1) >= .001:
                audio += f",atempo={speed:.6f}"
            audio_parts.append(f"{audio}[a{index}]")
        video_labels.append(f"[v{index}]")
        if keep_background:
            audio_labels.append(f"[a{index}]")
    count = len(pieces)
    video_parts.append(f"{''.join(video_labels)}concat=n={count}:v=1:a=0[{'vbase' if video_filter else 'v'}]")
    if video_filter:
        video_parts.append(f"[vbase]{video_filter}[v]")
    if keep_background:
        audio_parts.append(f"{''.join(audio_labels)}concat=n={count}:v=0:a=1[bg]")
        audio_parts.append(
            f"[bg]volume={background_volume:.3f}[bgv];[bgv][2:a]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]")
    return ";".join(video_parts + audio_parts)


def render_video(source_video: Path, instrumental: Path | None, voice: Path, destination: Path,
                 duration: float, background_volume: float, pieces: list[dict] | None = None,
                 watermark: dict | None = None, subtitles: dict | None = None,
                 subtitle_path: Path | None = None, log_path: Path | None = None) -> str:
    textfile = destination.with_suffix(".watermark.txt")
    active_watermark = bool(watermark and watermark.get("enabled") and str(watermark.get("text") or "").strip())
    if active_watermark:
        textfile.write_text(str(watermark["text"]).strip(), encoding="utf-8")
    try:
        watermark_vf = drawtext_filter(watermark, textfile) if active_watermark else None
    except Exception:
        textfile.unlink(missing_ok=True)
        raise
    try:
        subtitle_vf = None
        if subtitles and subtitles.get("enabled"):
            if subtitle_path is None or not subtitle_path.is_file():
                raise ValueError("启用字幕烧录时缺少对应字幕文件")
            if subtitle_path.parent.resolve() != destination.parent.resolve():
                raise ValueError("字幕文件应与成片保存在同一目录")
            subtitle_vf = subtitle_filter(subtitles, subtitle_path.name)
    except Exception:
        textfile.unlink(missing_ok=True)
        raise
    video_vf = ",".join(filter(None, (subtitle_vf, watermark_vf)))
    retime = bool(pieces) and any(abs(float(piece["speed"]) - 1) >= .001 for piece in pieces)
    inputs = ["-i", str(source_video)]
    if instrumental is not None:
        inputs += ["-i", str(instrumental)]
    inputs += ["-i", str(voice)]
    voice_input = "2:a" if instrumental is not None else "1:a"
    needs_encode = bool(retime or video_vf)
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex[:8]}.mp4")

    def encoder_options(encoder: str) -> list[str]:
        if encoder == "h264_nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                    "-rc", "vbr", "-cq", "18", "-b:v", "0", "-pix_fmt", "yuv420p"]
        if encoder == "libx264":
            return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-pix_fmt", "yuv420p"]
        return ["-c:v", "copy"]

    if retime:
        filters = ["-filter_complex", _retime_filter(pieces, background_volume, video_vf,
                                                     instrumental is not None)]
        maps = ["-map", "[v]", "-map", "[a]" if instrumental is not None else voice_input]
        failure = "配音视频调速混音失败："
    else:
        video_chain = f"[0:v]{video_vf}[v];" if video_vf else ""
        audio_chain = (f"[1:a]volume={background_volume:.3f}[bg];[bg][2:a]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]"
                       if instrumental is not None else "")
        filters = (["-filter_complex", video_chain + audio_chain] if video_chain or audio_chain else [])
        maps = ["-map", "[v]" if video_vf else "0:v:0",
                "-map", "[a]" if instrumental is not None else voice_input]
        failure = "配音视频混音失败："

    def log(message: str) -> None:
        if log_path is not None:
            with log_path.open("a", encoding="utf-8") as output:
                output.write(message + "\n")

    encoders = ("h264_nvenc", "libx264") if needs_encode else ("copy",)
    errors = []
    try:
        for encoder in encoders:
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       *inputs, *filters, *maps, *encoder_options(encoder), "-c:a", "aac",
                       "-t", str(duration), str(temporary)]
            log(f"视频编码：{encoder}，开始合成")
            try:
                result = subprocess.run(command, capture_output=True, text=True, check=False,
                                        cwd=destination.parent)
            except FileNotFoundError as exc:
                raise RuntimeError("未找到 FFmpeg，无法合成视频") from exc
            if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size > 0:
                temporary.replace(destination)
                log(f"视频编码：{encoder}，合成完成")
                return encoder
            errors.append(f"{encoder}: {result.stderr[-700:]}")
            temporary.unlink(missing_ok=True)
            if encoder == "h264_nvenc":
                log("NVENC 合成失败，改用 CPU 编码重试")
    finally:
        temporary.unlink(missing_ok=True)
        if active_watermark:
            textfile.unlink(missing_ok=True)
    raise RuntimeError(failure + " | ".join(errors))
