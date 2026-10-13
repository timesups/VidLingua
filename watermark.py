"""Persistent text watermark settings and FFmpeg drawtext filter."""

import json
import os
from pathlib import Path


DEFAULTS = {"enabled": False, "text": "", "position": "top_right", "opacity": 0.45}
POSITIONS = {"top_right", "top_left", "bottom_right", "bottom_left"}


def load_config(path: Path) -> dict:
    if not path.is_file():
        return dict(DEFAULTS)
    return {**DEFAULTS, **json.loads(path.read_text(encoding="utf-8"))}


def save_config(path: Path, config: dict) -> dict:
    value = {**DEFAULTS, **config}
    value["text"] = str(value["text"]).strip()
    if len(value["text"]) > 80:
        raise ValueError("水印文字最多 80 字")
    if value["position"] not in POSITIONS:
        raise ValueError("水印位置无效")
    if not 0.15 <= float(value["opacity"]) <= 1.0:
        raise ValueError("透明度须在 0.15–1 之间")
    if value["enabled"] and not value["text"]:
        raise ValueError("开启水印时请填写文字")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return value


def _font(text: str) -> Path | None:
    cjk = any("\u4e00" <= char <= "\u9fff" for char in text)
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = ([fonts / "msyh.ttc", fonts / "msyh.ttf", fonts / "simhei.ttf", fonts / "simsun.ttc"]
                  if cjk else []) + [fonts / "arial.ttf"]
    return next((path for path in candidates if path.is_file()), None)


def _filter_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")


def drawtext_filter(settings: dict, textfile: Path) -> str | None:
    if not settings.get("enabled"):
        return None
    text = str(settings.get("text") or "").strip()
    if not text:
        return None
    font = _font(text)
    if font is None and any("\u4e00" <= char <= "\u9fff" for char in text):
        raise RuntimeError("未找到中文水印字体")
    corner = settings.get("position", "top_right")
    horizontal = "+" if corner.endswith("right") else "-"
    vertical = "+" if corner.startswith("bottom") else "-"
    x = f"min(20,max(0,(w-tw)/2))+max(0,w-tw-40)*(1{horizontal}cos(2*PI*t/120))/2"
    y = f"min(20,max(0,(h-th)/2))+max(0,h-th-40)*(1{vertical}cos(2*PI*t/180))/2"
    opacity = float(settings.get("opacity", .45))
    font_arg = f"fontfile='{_filter_path(font)}':" if font else "font='Arial':"
    return (f"drawtext={font_arg}textfile='{_filter_path(textfile)}':expansion=none:"
            f"fontsize=h*0.025:fontcolor=white@{opacity}:"
            f"borderw=1:bordercolor=black@{opacity}:"
            f"shadowx=1:shadowy=1:shadowcolor=black@{opacity}:x='{x}':y='{y}'")
