"""Settings and FFmpeg filter for burning final Chinese subtitles."""

import json
from pathlib import Path


DEFAULTS = {"enabled": False, "mode": "zh", "font_size": 28,
            "margin_bottom": 42, "outline": 2}


def load_config(path: Path) -> dict:
    return {**DEFAULTS, **(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})}


def save_config(path: Path, value: dict) -> dict:
    config = {**DEFAULTS, **value}
    if config["mode"] not in {"zh", "bilingual"}:
        raise ValueError("字幕模式须为中文或双语")
    for name, low, high in (("font_size", 14, 60), ("margin_bottom", 0, 200),
                            ("outline", 0, 5)):
        item = config[name]
        if isinstance(item, bool) or not isinstance(item, int) or not low <= item <= high:
            raise ValueError(f"{name} 必须在 {low}–{high} 之间")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return config


def subtitle_filter(settings: dict, filename: str) -> str | None:
    if not settings.get("enabled"):
        return None
    if not filename or any(char in filename for char in ("'", ":", "\\", "/", ",", ";")):
        raise ValueError("字幕文件名不适合 FFmpeg 滤镜")
    style = (f"FontName=Microsoft YaHei,FontSize={settings['font_size']},"
             f"Outline={settings['outline']},MarginV={settings['margin_bottom']},"
             "Alignment=2,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000")
    return f"subtitles=filename='{filename}':charenc=UTF-8:force_style='{style}'"
