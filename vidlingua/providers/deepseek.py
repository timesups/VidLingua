"""DeepSeek settings and ID-preserving English-to-Chinese subtitle translation."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

import keyring
import requests


API_URL = "https://api.deepseek.com/chat/completions"
SERVICE = "VidLingua DeepSeek"
ACCOUNT = "local-api-key"
DEFAULTS = {"model": "deepseek-flash", "temperature": 0.2, "max_tokens": 4096,
            "concurrency": 1}
MODELS = {"deepseek-flash", "deepseek-v4-pro"}
GLOSSARY_VERSION = 2
TRANSLATION_VERSION = 3
CORRECTION_VERSION = 1


def load_config(path: Path) -> dict:
    if not path.is_file():
        return dict(DEFAULTS)
    saved = json.loads(path.read_text(encoding="utf-8"))
    return {**DEFAULTS, **{name: value for name, value in saved.items() if name in DEFAULTS}}


def save_config(path: Path, config: dict) -> None:
    if config["model"] not in MODELS:
        raise ValueError("不支持的 DeepSeek 模型")
    if not 0 <= config["temperature"] <= 2:
        raise ValueError("温度必须在 0–2 之间")
    if not 512 <= config["max_tokens"] <= 8192:
        raise ValueError("最大输出长度必须在 512–8192 之间")
    concurrency = config["concurrency"]
    if isinstance(concurrency, bool) or int(concurrency) != concurrency or not 1 <= concurrency <= 8:
        raise ValueError("DeepSeek 并发数必须在 1–8 之间")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def get_api_key() -> str | None:
    return keyring.get_password(SERVICE, ACCOUNT) or os.environ.get("DEEPSEEK_API_KEY")


def validate_api_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise ValueError("DeepSeek API Key 不能为空")
    if any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("DeepSeek API Key 含中文、空格或其他非 ASCII 字符；请只粘贴平台生成的密钥，不要附带说明文字")
    return key


def set_api_key(value: str) -> None:
    keyring.set_password(SERVICE, ACCOUNT, validate_api_key(value))


def delete_api_key() -> None:
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        pass


def call_deepseek(api_key: str, config: dict, messages: list[dict]) -> dict:
    api_key = validate_api_key(api_key)
    response = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": config["model"], "messages": messages,
              "thinking": {"type": "disabled"},
              "temperature": config["temperature"], "max_tokens": config["max_tokens"],
              "response_format": {"type": "json_object"}, "stream": False},
        timeout=(10, 180),
    )
    if not response.ok:
        raise RuntimeError(f"DeepSeek 请求失败：HTTP {response.status_code}")
    result = response.json()
    choices = result.get("choices") or []
    if not choices or choices[0].get("finish_reason") != "stop":
        raise RuntimeError("DeepSeek 响应未完整结束，请重试")
    content = choices[0].get("message", {}).get("content")
    if not content:
        raise RuntimeError("DeepSeek 返回空内容，请重试")
    try:
        return {"content": json.loads(content), "usage": result.get("usage", {})}
    except json.JSONDecodeError as exc:
        raise RuntimeError("DeepSeek 返回的内容不是有效 JSON") from exc


SYSTEM_PROMPT = """You translate English lecture subtitles into concise, natural spoken Mandarin Chinese.
Preserve technical meaning, names, software commands, numbers, units, and course terminology. Use the supplied glossary consistently. Do not invent unclear words. Fit each line to its time budget without dropping concepts.
For genuinely ambiguous Chinese polyphonic characters or names, add pronunciations with exact text spans, numbered Pinyin, and a same-sounding Chinese homophone alias for speech engines. Example: {"text":"行","pinyin":"hang2","alias":"航"}. Omit obvious readings and never change the written translation to a homophone.
Return JSON only. Example JSON: {"segments":[{"id":"s0001","zh":"这是一个例子。","uncertain":false,"pronunciations":[]}]}.
Return exactly one item for every input ID in the same order. If source wording is uncertain, provide the best translation and set uncertain=true.
Do not include timestamps in the response."""


def correct_english_group(api_key: str, config: dict, packet: dict, group: dict,
                          next_context: str = "") -> tuple[list[dict], dict]:
    """Correct recognition errors using neighboring lecture context without changing IDs."""
    expected = [item["id"] for item in group["segments"]]
    payload = {"course": packet["title"], "previous_context": group["previous_context"],
               "next_context": next_context,
               "segments": [{"id": item["id"], "en": item["edited_text"]}
                            for item in group["segments"]]}
    messages = [{"role": "system", "content":
                 "Correct English lecture transcript recognition errors using context. Keep the speaker's meaning, "
                 "technical terms, names and sentence boundaries. Make only well-supported corrections; do not invent "
                 "inaudible words, translate, merge, split, or reorder segments. Return JSON only as "
                 '{"segments":[{"id":"s0001","en":"Corrected English sentence.","uncertain":false}]}. '
                 "Return every supplied ID exactly once in the same order."},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    for attempt in range(2):
        response = call_deepseek(api_key, config, messages)
        items = response["content"].get("segments") if isinstance(response["content"], dict) else None
        if isinstance(items, list) and [item.get("id") for item in items if isinstance(item, dict)] == expected:
            if all(isinstance(item.get("en"), str) and item["en"].strip() for item in items):
                return ([{"id": item["id"], "en": " ".join(item["en"].split()),
                          "uncertain": bool(item.get("uncertain"))} for item in items], response["usage"])
        if attempt == 0:
            messages.append({"role": "user", "content": "Return corrected JSON with these IDs in order: "
                             + ", ".join(expected)})
    raise RuntimeError(f"组 {group['id']} 的英文校正 ID 或正文不完整")


def clean_pronunciations(chinese: str, value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        target, pinyin, alias = item.get("text"), item.get("pinyin"), item.get("alias")
        if not all(isinstance(part, str) for part in (target, pinyin, alias)):
            continue
        target, pinyin, alias = target.strip(), pinyin.strip().lower(), alias.strip()
        if not target or target not in chinese or len(target) > 8 or not re.fullmatch(r"[\u4e00-\u9fff]+", target):
            continue
        if not re.fullmatch(r"[a-z]+[1-5](?:\s+[a-z]+[1-5])*", pinyin):
            continue
        if (len(pinyin.split()) != len(target) or alias == target
                or not re.fullmatch(r"[\u4e00-\u9fff]{1,8}", alias)):
            continue
        cleaned.append({"text": target, "pinyin": pinyin, "alias": alias})
    return cleaned


def glossary_candidates(packet: dict, limit: int = 50) -> list[dict]:
    """Find recurring terms and names; DeepSeek decides which are true domain terms."""
    stop = {"the", "and", "for", "are", "can", "has", "not", "but", "all", "out",
            "get", "one", "two", "any", "see", "let", "take", "use", "using",
            "about", "after", "again", "also", "another", "because", "before", "between",
            "could", "every", "first", "from", "have", "here", "into", "just", "like",
            "more", "most", "much", "other", "really", "some", "that", "their", "them",
            "there", "these", "this", "those", "through", "very", "what", "when", "where",
            "which", "while", "with", "would", "your", "you", "then", "than", "they",
            "will", "were", "been", "being", "look", "make", "thing", "things", "video",
            "time", "right", "well", "down", "over", "under", "same", "like", "want"}
    texts = [s["edited_text"] for g in packet["groups"] for s in g["segments"]]
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    display: dict[str, str] = {}
    for sentence in texts:
        words = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", sentence)
        for n in (1, 2, 3):
            for pos in range(len(words) - n + 1):
                phrase = words[pos:pos + n]
                if any(len(word) < 3 or word.lower() in stop for word in phrase):
                    continue
                name = " ".join(phrase)
                key = name.lower()
                counts[key] += 1
                examples.setdefault(key, sentence[:240])
                display.setdefault(key, name)
    ranked = []
    for key, count in counts.items():
        proper = bool(re.search(r"\b[A-Z][a-z]+\b", display[key]))
        if count < 2 and not proper:
            continue
        score = count * (1 + 0.35 * key.count(" "))
        ranked.append((score, key))
    ranked.sort(reverse=True)
    return [{"en": display[key], "count": counts[key], "example": examples[key]}
            for _, key in ranked[:limit]]


def refine_glossary(api_key: str, config: dict, packet: dict, candidates: list[dict]) -> tuple[str, list[dict], dict]:
    texts = [s["edited_text"] for g in packet["groups"] for s in g["segments"]]
    sample = []
    if texts:
        indexes = sorted({round(i * (len(texts) - 1) / min(29, len(texts) - 1))
                          for i in range(min(30, len(texts)))}) if len(texts) > 1 else [0]
        sample = [texts[i][:350] for i in indexes]
    payload = {"course": packet["title"], "lecture_samples": sample, "candidates": candidates}
    system = """Analyze an English lecture before translating subtitles into Chinese.
From the lecture samples, write a short Chinese description of the actual subject and context (at most 120 Chinese characters). Be specific but do not invent facts. If uncertain, describe only what is clear.
Select only genuine domain terms, product names, or recurring technical phrases from the supplied candidates. For each selected English term, provide one concise, context-appropriate Chinese rendering. Keep brand and product names in English when appropriate. Do not invent terms.
Return JSON only: {"subject":"简短课程背景","terms":[{"en":"procedural shader","zh":"程序化着色器"}]}.
Every en value must exactly match a supplied candidate. Omit common words and uncertain terms."""
    response = call_deepseek(api_key, config, [
        {"role": "system", "content": system},
        {"role": "user", "content": "Analyze the course and create a glossary. Return JSON.\n"
         + json.dumps(payload, ensure_ascii=False)},
    ])
    data = response["content"]
    terms = data.get("terms") if isinstance(data, dict) else None
    if not isinstance(terms, list):
        raise RuntimeError("DeepSeek 术语整理未返回 terms 列表")
    subject = data.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise RuntimeError("DeepSeek 未返回课程背景")
    subject = " ".join(subject.split())[:160]
    allowed = {item["en"].casefold(): item["en"] for item in candidates}
    result, seen = [], set()
    for item in terms:
        if not isinstance(item, dict):
            continue
        english, chinese = item.get("en"), item.get("zh")
        if not isinstance(english, str) or not isinstance(chinese, str):
            continue
        key = english.strip().casefold()
        if key not in allowed or key in seen or not chinese.strip():
            continue
        seen.add(key)
        result.append({"en": allowed[key], "zh": " ".join(chinese.split()), "source": "deepseek"})
    return subject, result, response["usage"]


def translate_group(api_key: str, config: dict, packet: dict, group: dict) -> tuple[list[dict], dict]:
    expected = [segment["id"] for segment in group["segments"]]
    payload = {
        "course": packet["title"], "subject": packet["subject"],
        "glossary": packet["glossary"], "previous_context": group["previous_context"],
        "translation_rules": packet.get("translation_rules", []),
        "segments": [{"id": s["id"], "en": s["edited_text"],
                      "duration_seconds": s["duration_seconds"], "review_flags": s["review_flags"]}
                     for s in group["segments"]],
    }
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Translate this group. Return a JSON object.\n" + json.dumps(payload, ensure_ascii=False)}]
    for attempt in range(2):
        response = call_deepseek(api_key, config, messages)
        items = response["content"].get("segments") if isinstance(response["content"], dict) else None
        if isinstance(items, list) and [item.get("id") for item in items if isinstance(item, dict)] == expected:
            if all(isinstance(item.get("zh"), str) and item["zh"].strip() for item in items):
                result = []
                for item, source in zip(items, group["segments"]):
                    chinese = " ".join(item["zh"].split())
                    flags = []
                    if bool(item.get("uncertain")):
                        flags.append("translation_uncertain")
                    if len(re.sub(r"\s", "", chinese)) / max(source["duration_seconds"], 0.1) > 7:
                        flags.append("fast_reading")
                    result.append({"id": item["id"], "start": source["start"], "end": source["end"],
                                   "en": source["edited_text"], "zh": chinese, "flags": flags,
                                   "pronunciations": clean_pronunciations(chinese, item.get("pronunciations"))})
                return result, response["usage"]
        if attempt == 1:
            raise RuntimeError(f"组 {group['id']} 的译文 ID 或正文不完整")
        messages.append({"role": "user", "content": "The prior answer did not match every ID. Return corrected JSON only, with IDs in this exact order: " + ", ".join(expected)})
    raise AssertionError("unreachable")


def srt_time(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def make_srt(items: list[dict]) -> str:
    voiced = [item for item in items if str(item.get("zh") or "").strip()]
    return "\n\n".join(f"{i}\n{srt_time(item['start'])} --> {srt_time(item['end'])}\n{item['zh']}"
                      for i, item in enumerate(voiced, 1)) + ("\n" if voiced else "")


def make_bilingual_srt(items: list[dict]) -> str:
    """Put Chinese above the source English in each timed cue."""
    voiced = [item for item in items if str(item.get("zh") or "").strip()]
    return "\n\n".join(
        f"{i}\n{srt_time(item['start'])} --> {srt_time(item['end'])}\n"
        f"{' '.join(item['zh'].split())}\n{' '.join(item['en'].split())}"
        for i, item in enumerate(voiced, 1)
    ) + ("\n" if voiced else "")


def make_english_srt(items: list[dict]) -> str:
    return "\n\n".join(f"{i}\n{srt_time(item['start'])} --> {srt_time(item['end'])}\n{item['en']}"
                      for i, item in enumerate(items, 1)) + "\n"
