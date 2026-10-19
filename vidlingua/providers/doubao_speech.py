"""Doubao (Volcengine) speech synthesis for Chinese dubbing."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import uuid
import wave
from pathlib import Path

import keyring
import numpy as np
import requests

from .azure_speech import SAMPLE_RATE

SERVICE = "VidLingua Doubao Speech"
ACCOUNT = "local-api-key"
ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
RESOURCE_IDS = ("seed-tts-2.0", "seed-tts-1.0", "seed-tts-1.0-concurr")
VOICES = {
    "zh_female_vv_uranus_bigtts": "Vivi · 女声 · 2.0",
    "zh_female_xiaohe_uranus_bigtts": "小何 · 女声 · 2.0",
    "zh_female_shuangkuaisisi_moon_bigtts": "爽快思思 · 女声 · 1.0",
}
DEFAULTS = {"resource_id": "seed-tts-2.0", "speaker": "zh_female_vv_uranus_bigtts",
            "concurrency": 1, "calmness": 2}
CALM_INSTRUCTIONS = {
    1: "语气平稳一些，情绪起伏小一点。",
    2: "用平静、平稳的语气朗读，减少情绪起伏。",
    3: "用非常平静、克制的语气平铺直叙，几乎不要情绪起伏。",
}
SYNTHESIS_MAX_ATTEMPTS = 5
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


def load_config(path: Path) -> dict:
    saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {**DEFAULTS, **{key: value for key, value in saved.items() if key in DEFAULTS}}


def validate_speaker(speaker: str) -> str:
    speaker = speaker.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{2,80}", speaker):
        raise ValueError("豆包发音人 ID 只能包含字母、数字和下划线")
    return speaker


def validate_config(config: dict) -> dict:
    resource_id = str(config["resource_id"]).strip()
    if resource_id not in RESOURCE_IDS:
        raise ValueError("豆包资源 ID 只能是 seed-tts-2.0、seed-tts-1.0 或 seed-tts-1.0-concurr")
    concurrency = config["concurrency"]
    if isinstance(concurrency, bool) or int(concurrency) != concurrency or not 1 <= int(concurrency) <= 8:
        raise ValueError("豆包并发数必须在 1–8 之间")
    calmness = config.get("calmness", 0)
    if isinstance(calmness, bool) or int(calmness) != calmness or not 0 <= int(calmness) <= 3:
        raise ValueError("豆包平静程度必须在 0–3 之间")
    return {**config, "resource_id": resource_id, "speaker": validate_speaker(str(config["speaker"])),
            "concurrency": int(concurrency), "calmness": int(calmness)}


def save_config(path: Path, config: dict) -> None:
    config = validate_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def validate_key(key: str) -> str:
    key = key.strip()
    if not key or any(ord(char) < 33 or ord(char) > 126 for char in key) or not 8 <= len(key) <= 256:
        raise ValueError("豆包 API Key 格式不正确；请只粘贴控制台中的密钥")
    return key


def get_key() -> str | None:
    stored = keyring.get_password(SERVICE, ACCOUNT) or os.environ.get("DOUBAO_SPEECH_API_KEY")
    return validate_key(stored) if stored else None


def set_key(key: str) -> None:
    keyring.set_password(SERVICE, ACCOUNT, validate_key(key))


def delete_key() -> None:
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        pass


def speech_rate(rate: float) -> int:
    """Map a 1.0-based speed to Doubao's scale, where 0 is unchanged and 100 is 2x."""
    return int(max(-50, min(100, round((float(rate) - 1) * 100))))


def parse_chunks(lines) -> bytes:
    audio = bytearray()
    finished = False
    for raw in lines:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        text = text.strip()
        if text.startswith("data:"):
            text = text[5:].strip()
        if not text.startswith("{"):
            continue
        payload = json.loads(text)
        try:
            code = int(payload.get("code", 0))
        except (TypeError, ValueError):
            code = -1
        if code == 20000000:
            finished = True
            break
        if code != 0:
            message = payload.get("message") or f"code {code}"
            raise RuntimeError(f"豆包语音合成失败：{message}")
        data = payload.get("data")
        if data:
            audio.extend(base64.b64decode(data))
    if not finished:
        raise RuntimeError("豆包语音流在结束前中断")
    if not audio:
        raise RuntimeError("豆包语音返回了空音频")
    if len(audio) % 2:
        del audio[-1:]
    return bytes(audio)


def pcm_to_wav(pcm: bytes) -> bytes:
    frames = np.frombuffer(pcm, dtype="<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames.tobytes())
    return output.getvalue()


def _calm_fields(config: dict) -> tuple[dict, dict]:
    """2.0 voices take a spoken instruction; 1.0 voices take a neutral emotion."""
    level = int(config.get("calmness") or 0)
    if level <= 0:
        return {}, {}
    level = min(3, level)
    if str(config.get("resource_id") or "").startswith("seed-tts-1.0"):
        return {"emotion": "neutral", "emotion_scale": 4 - level}, {}
    return {}, {"context_texts": [CALM_INSTRUCTIONS[level]]}


def synthesize(key: str, config: dict, text: str, rate: float = 1.0,
               annotations: list[dict] | None = None) -> bytes:
    del annotations
    checked = validate_config(config)
    audio_extra, addition_extra = _calm_fields(checked)
    payload = {
        "user": {"uid": "vidlingua"},
        "req_params": {
            "text": text,
            "speaker": checked["speaker"],
            "audio_params": {
                "format": "pcm",
                "sample_rate": SAMPLE_RATE,
                "speech_rate": speech_rate(rate),
                **audio_extra,
            },
            "additions": json.dumps({
                "explicit_language": "zh-cn",
                "max_length_to_filter_parenthesis": 0,
                "disable_markdown_filter": True,
                "silence_duration": 80,
                **addition_extra,
            }, ensure_ascii=False),
        },
    }
    headers = {
        "X-Api-Key": validate_key(key),
        "X-Api-Resource-Id": checked["resource_id"],
        "X-Api-Request-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    response = _post_with_retry(headers, payload)
    pcm = parse_chunks(response.iter_lines())
    return pcm_to_wav(pcm)


def _post_with_retry(headers: dict, payload: dict):
    last_error = "豆包语音没有响应"
    for attempt in range(SYNTHESIS_MAX_ATTEMPTS):
        try:
            response = requests.post(ENDPOINT, headers=headers, json=payload, stream=True, timeout=(10, 120))
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as exc:
            last_error = str(exc)
            if attempt == SYNTHESIS_MAX_ATTEMPTS - 1:
                raise RuntimeError(f"豆包语音连接失败，已尝试 {SYNTHESIS_MAX_ATTEMPTS} 次：{exc}") from exc
            time.sleep(min(30, 2 ** attempt))
            continue
        if response.status_code in RETRYABLE_HTTP_STATUS and attempt < SYNTHESIS_MAX_ATTEMPTS - 1:
            response.close()
            time.sleep(min(30, 2 ** attempt))
            continue
        if not response.ok:
            detail = response.text[:300]
            response.close()
            raise RuntimeError(f"豆包语音合成失败：HTTP {response.status_code} {detail}")
        return response
    raise RuntimeError(last_error)
