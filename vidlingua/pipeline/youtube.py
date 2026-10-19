"""Validate and download one YouTube video for the existing MP4 pipeline."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from .import_media import probe


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
PROJECT_DATA = Path(__file__).resolve().parents[2] / ".web_data"
CONFIG_PATH = PROJECT_DATA / "youtube_config.json"
COOKIE_PATH = PROJECT_DATA / "youtube_cookies.txt"
DEFAULTS = {"proxy_mode": "system", "proxy_port": ""}


def validate_config(values: dict) -> dict:
    mode = str(values.get("proxy_mode", "system")).strip()
    port = str(values.get("proxy_port", "")).strip()
    if mode not in {"system", "direct", "local"}:
        raise ValueError("代理方式不正确")
    if port and (not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535):
        raise ValueError("本机代理端口必须为 1–65535")
    if mode == "local" and not port:
        raise ValueError("指定本机代理时必须填写端口")
    return {"proxy_mode": mode, "proxy_port": port}


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.is_file():
        return dict(DEFAULTS)
    return validate_config({**DEFAULTS, **json.loads(path.read_text(encoding="utf-8"))})


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_config(path: Path, values: dict) -> dict:
    config = validate_config(values)
    _atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return config


def save_cookies(path: Path, content: str) -> None:
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("cookies.txt 不能超过 2 MB")
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").strip()
    if not normalized.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        raise ValueError("请导入 Netscape 格式的 cookies.txt 文件")
    entries = [line for line in normalized.splitlines()
               if line and (not line.startswith("#") or line.startswith("#HttpOnly_"))]
    if not entries or any(len(line.split("\t")) != 7 for line in entries):
        raise ValueError("cookies.txt 未包含有效的 Netscape Cookie 记录")
    _atomic_write(path, normalized + "\n")


def ydl_network_options(config_path: Path = CONFIG_PATH, cookie_path: Path = COOKIE_PATH) -> dict:
    config = load_config(config_path)
    options = {"js_runtimes": {"node": {}}, "socket_timeout": 30}
    if config["proxy_mode"] == "direct":
        options["proxy"] = ""
    elif config["proxy_mode"] == "local":
        options["proxy"] = f"http://127.0.0.1:{config['proxy_port']}"
    if cookie_path.is_file() and cookie_path.stat().st_size:
        options["cookiefile"] = str(cookie_path)
    return options


def test_connection(config_path: Path = CONFIG_PATH, cookie_path: Path = COOKIE_PATH) -> dict:
    import yt_dlp

    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    options = {**ydl_network_options(config_path, cookie_path), "quiet": True,
               "no_warnings": True, "noplaylist": True, "retries": 0}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or info.get("id") != "jNQXAC9IVRw":
        raise RuntimeError("YouTube 返回了意外的视频信息")
    return {"ok": True, "video_id": info["id"], "title": str(info.get("title") or "")}


def canonical_youtube_url(value: str) -> tuple[str, str]:
    """Accept single-video URLs only; never pass an arbitrary URL to yt-dlp."""
    if not value or re.search(r"[\\\x00-\x20\x7f]", value.strip()):
        raise ValueError("请输入有效的 YouTube 单视频链接")
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("请输入有效的 YouTube 单视频链接") from exc
    if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)
            or parsed.netloc.endswith(":") or host.lower() not in YOUTUBE_HOSTS | {"youtu.be"}):
        raise ValueError("仅支持 HTTPS YouTube 单视频链接")
    path = parsed.path.strip("/")
    parts = path.split("/") if path else []
    if host.lower() == "youtu.be":
        video_id = parts[0] if len(parts) == 1 else ""
    elif parts == ["watch"]:
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
        video_id = parts[1]
    else:
        video_id = ""
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("仅支持 YouTube 单视频链接，不支持播放列表或频道")
    return f"https://www.youtube.com/watch?v={video_id}", video_id


def _thumbnail_url(info: dict) -> str:
    candidates = []
    for item in info.get("thumbnails") or []:
        url = str(item.get("url") or "")
        if url.startswith("http"):
            candidates.append((int(item.get("width") or 0), url))
    if candidates:
        return max(candidates)[1]
    url = str(info.get("thumbnail") or "")
    return url if url.startswith("http") else ""


def _thumbnail_session(config_path: Path) -> requests.Session:
    config = load_config(config_path)
    session = requests.Session()
    if config["proxy_mode"] == "direct":
        session.trust_env = False
    elif config["proxy_mode"] == "local":
        proxy = f"http://127.0.0.1:{config['proxy_port']}"
        session.proxies.update({"http": proxy, "https": proxy})
        session.trust_env = False
    return session


def save_youtube_cover(info: dict, cover_path: Path, config_path: Path = CONFIG_PATH) -> bool:
    """Save the largest YouTube thumbnail as a JPEG cover. Missing art is not an error."""
    url = _thumbnail_url(info)
    if not url or (cover_path.is_file() and cover_path.stat().st_size > 0):
        return cover_path.is_file()
    response = _thumbnail_session(config_path).get(url, timeout=30)
    response.raise_for_status()
    data = response.content
    if not data:
        return False
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    if data[:2] == b"\xff\xd8":
        cover_path.write_bytes(data)
        return True
    raw = cover_path.with_suffix(".source")
    raw.write_bytes(data)
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", str(raw), "-frames:v", "1", "-q:v", "3", str(cover_path)],
            capture_output=True, check=False)
    finally:
        raw.unlink(missing_ok=True)
    return result.returncode == 0 and cover_path.is_file() and cover_path.stat().st_size > 0


def ensure_youtube_cover(url: str, cover_path: Path, config_path: Path = CONFIG_PATH,
                         cookie_path: Path = COOKIE_PATH) -> bool:
    if cover_path.is_file() and cover_path.stat().st_size > 0:
        return True
    import yt_dlp

    canonical, _video_id = canonical_youtube_url(url)
    options = {**ydl_network_options(config_path, cookie_path), "quiet": True,
               "no_warnings": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(canonical, download=False)
    if not info:
        return False
    return save_youtube_cover(info, cover_path, config_path)


def download_youtube(url: str, destination: Path, log_path: Path,
                     config_path: Path = CONFIG_PATH, cookie_path: Path = COOKIE_PATH) -> dict:
    """Download into a staging file, verify streams, then publish the MP4."""
    import yt_dlp

    canonical, video_id = canonical_youtube_url(url)
    if destination.exists() and destination.stat().st_size:
        _verify_video(destination)
        return {"id": video_id, "title": ""}
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name("youtube-download.mp4")
    last_percent = -5

    class JobLogger:
        def warning(self, message):
            with log_path.open("a", encoding="utf-8") as output:
                output.write(f"YouTube: {message}\n")

        def error(self, message):
            self.warning(message)

        def debug(self, message):
            pass

    def progress(status):
        nonlocal last_percent
        if status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        if not total:
            return
        percent = min(99, int(100 * status.get("downloaded_bytes", 0) / total))
        if percent >= last_percent + 5:
            last_percent = percent
            with log_path.open("a", encoding="utf-8") as output:
                output.write(f"YouTube: {percent}%\n")

    options = {
        **ydl_network_options(config_path, cookie_path),
        "format": ("bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
                   "best[height<=1080][ext=mp4]/bestvideo[height<=1080]+bestaudio/"
                   "best[height<=1080]/best"),
        "outtmpl": str(staging.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "logger": JobLogger(),
        "progress_hooks": [progress],
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(canonical, download=True)
        if not info or str(info.get("id")) != video_id:
            raise RuntimeError("YouTube 返回的视频与提交的链接不一致")
        try:
            if not save_youtube_cover(info, destination.with_name("cover.jpg"), config_path):
                with log_path.open("a", encoding="utf-8") as output:
                    output.write("YouTube: 未找到可保存的封面\n")
        except Exception as exc:
            with log_path.open("a", encoding="utf-8") as output:
                output.write(f"YouTube: 封面下载失败：{exc}\n")
        if not staging.is_file() or staging.stat().st_size == 0:
            raise RuntimeError("YouTube 下载未生成 MP4 文件")
        _verify_video(staging)
        staging.replace(destination)
        with log_path.open("a", encoding="utf-8") as output:
            output.write(f"YouTube: 下载完成，{destination.stat().st_size / 1024 / 1024:.1f} MB\n")
        return {"id": video_id, "title": str(info.get("title") or "").strip()}
    except yt_dlp.utils.DownloadError as exc:
        detail = str(exc)
        if "403" in detail:
            detail += "；如视频需要登录，请检查网络或配置 YouTube cookies"
        raise RuntimeError(f"YouTube 下载失败：{detail}") from exc


def _verify_video(path: Path) -> None:
    metadata = probe(path)
    kinds = {stream.get("codec_type") for stream in metadata.get("streams", [])}
    if not {"video", "audio"}.issubset(kinds):
        raise RuntimeError("下载的视频缺少画面或音轨")
