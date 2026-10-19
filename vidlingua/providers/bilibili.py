"""Bilibili login, Chinese title/description, and video upload."""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import requests

from .deepseek import call_deepseek


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULTS = {"tid": 229, "tags": "配音,翻译,教程"}
PARTITIONS = [
    {"tid": 201, "name": "科学科普", "group": "知识"},
    {"tid": 124, "name": "社科·法律·心理", "group": "知识"},
    {"tid": 228, "name": "人文历史", "group": "知识"},
    {"tid": 207, "name": "财经商业", "group": "知识"},
    {"tid": 208, "name": "校园学习", "group": "知识"},
    {"tid": 209, "name": "职业职场", "group": "知识"},
    {"tid": 229, "name": "设计·创意", "group": "知识"},
    {"tid": 122, "name": "野生技能协会", "group": "知识"},
    {"tid": 95, "name": "数码", "group": "科技"},
    {"tid": 230, "name": "软件应用", "group": "科技"},
    {"tid": 231, "name": "计算机技术", "group": "科技"},
    {"tid": 17, "name": "单机游戏", "group": "游戏"},
    {"tid": 171, "name": "电子竞技", "group": "游戏"},
    {"tid": 172, "name": "手机游戏", "group": "游戏"},
    {"tid": 21, "name": "日常", "group": "生活"},
    {"tid": 85, "name": "短片·手书·配音", "group": "影视"},
    {"tid": 182, "name": "影视杂谈", "group": "影视"},
    {"tid": 183, "name": "影视剪辑", "group": "影视"},
]
PARTITION_TIDS = {item["tid"] for item in PARTITIONS}
_uploads: dict[str, dict] = {}
_upload_lock = threading.Lock()


def load_config(path: Path) -> dict:
    if not path.is_file():
        return dict(DEFAULTS)
    saved = json.loads(path.read_text(encoding="utf-8"))
    return {**DEFAULTS, **{name: saved[name] for name in DEFAULTS if name in saved}}


def save_config(path: Path, config: dict) -> None:
    tid = int(config["tid"])
    if tid not in PARTITION_TIDS:
        raise ValueError("请选择支持的投稿分区")
    tags = tag_text(config.get("tags") or DEFAULTS["tags"])
    if not tags:
        raise ValueError("请至少填写一个标签")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"tid": tid, "tags": tags}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    temporary.replace(path)


def parse_cookie_string(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data.get("cookies"), dict):
            data = data["cookies"]
        return {str(key): str(value) for key, value in data.items()}
    cookies = {}
    for part in re.split(r";\s*", raw):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def load_cookies(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, dict):
        return {}
    return {str(key): str(value) for key, value in cookies.items()}


def save_cookies(path: Path, cookies: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"cookies": cookies}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def clear_cookies(path: Path) -> None:
    if path.is_file():
        path.unlink()


def _session(cookies: dict[str, str]) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://member.bilibili.com/",
        "Origin": "https://member.bilibili.com",
    })
    session.cookies.update(cookies)
    return session


def login_status(cookie_path: Path) -> dict:
    cookies = load_cookies(cookie_path)
    if not cookies.get("SESSDATA") or not cookies.get("bili_jct"):
        return {"logged_in": False, "message": "未登录"}
    response = _session(cookies).get("https://api.bilibili.com/x/web-interface/nav", timeout=20)
    response.raise_for_status()
    payload = response.json()
    info = payload.get("data") or {}
    if payload.get("code") != 0 or not info.get("isLogin"):
        return {"logged_in": False, "message": "登录已失效，请重新粘贴 Cookie"}
    return {"logged_in": True, "uname": info.get("uname") or "", "mid": info.get("mid")}


def save_cookie_login(cookie_path: Path, raw: str) -> dict:
    cookies = parse_cookie_string(raw)
    missing = [name for name in ("SESSDATA", "bili_jct") if not cookies.get(name)]
    if missing:
        raise ValueError("缺少必要 Cookie：" + "、".join(missing))
    save_cookies(cookie_path, cookies)
    status = login_status(cookie_path)
    if not status["logged_in"]:
        clear_cookies(cookie_path)
        raise ValueError(status.get("message") or "Cookie 无效或已过期")
    return status


def plain_subtitles(srt: str) -> str:
    lines = []
    for line in srt.lstrip("\ufeff").splitlines():
        text = line.strip()
        if not text or text.isdigit() or "-->" in text:
            continue
        lines.append(text)
    return "\n".join(lines)


def tag_text(value: Any) -> str:
    if isinstance(value, list):
        parts = [str(item).strip().lstrip("#") for item in value]
    else:
        parts = re.split(r"[,，]", str(value or ""))
    tags = []
    for part in parts:
        tag = part.strip().lstrip("#")
        if tag and tag not in tags:
            tags.append(tag[:20])
        if len(tags) == 10:
            break
    return ",".join(tags)


def normalize_meta(data: dict, *, filename: str) -> dict:
    title = str(data.get("title") or "").strip() or Path(filename).stem[:80] or "配音视频"
    desc = str(data.get("desc") or "").replace("\\n", "\n").strip()
    dynamic = str(data.get("dynamic") or "").strip()[:100]
    return {"title": title[:80], "desc": desc[:2000], "tags": tag_text(data.get("tag") or data.get("tags")),
            "dynamic": dynamic}


def append_source_link(desc: str, source_url: str | None) -> str:
    text = (desc or "").strip()
    url = (source_url or "").strip()
    if not text or "youtube.com" not in url and "youtu.be" not in url:
        return text[:2000]
    marker = f"原视频：{url}"
    if url in text or "原视频：" in text:
        return text[:2000]
    if len(text) + len(marker) + 2 > 2000:
        text = text[: 2000 - len(marker) - 2].rstrip()
    return f"{text}\n\n{marker}" if text else marker[:2000]


def _fallback_meta(filename: str, subtitle_text: str, original_title: str) -> dict:
    title = (original_title or Path(filename).stem or "配音视频")[:80]
    summary = " ".join(subtitle_text.splitlines()[:12]).strip()[:600]
    desc = f"本视频为《{title}》的中文配音版本。\n\n内容概要：{summary or '详见视频与字幕。'}"
    return normalize_meta({"title": title, "desc": desc, "tag": ["配音", "翻译", "教程"],
                           "dynamic": f"分享《{title}》中文配音版"}, filename=filename)


def generate_meta(api_key: str, config: dict, *, filename: str, subtitle_text: str,
                  original_title: str = "", source_url: str = "") -> dict:
    clipped = subtitle_text.strip()
    if len(clipped) > 6000:
        clipped = clipped[:6000] + "\n…"
    title_block = f"原视频标题：{original_title.strip()}\n\n" if original_title.strip() else ""
    system = (
        "你是 B 站视频投稿助手。根据原视频标题和中文字幕生成投稿元数据。"
        "只输出一个 JSON 对象，字段为 title、desc、tag、dynamic。"
        "title 为不超过 80 字的中文标题，贴合原标题主题，适合 B 站；"
        "desc 为 200-800 字中文简介，可用换行；"
        "tag 为 3-10 个不带 # 的标签数组；dynamic 不超过 50 字。"
    )
    user = (f"{title_block}视频文件名：{filename}\n\n字幕内容：\n{clipped}\n\n"
            '只输出 JSON：{"title":"...","desc":"...","tag":["..."],"dynamic":"..."}')
    try:
        data = call_deepseek(api_key, {**config, "temperature": 0.3},
                             [{"role": "system", "content": system},
                              {"role": "user", "content": user}])["content"]
        if not isinstance(data, dict):
            raise RuntimeError("返回内容不是对象")
        meta = normalize_meta(data, filename=filename)
    except Exception:
        meta = _fallback_meta(filename, clipped, original_title)
        meta["warning"] = "模型结果无法使用，已根据字幕生成简要标题和简介"
    else:
        meta["warning"] = ""
    meta["desc"] = append_source_link(meta["desc"], source_url)
    return meta


def empty_meta(settings: dict) -> dict:
    return {"title": "", "desc": "", "tags": settings.get("tags") or DEFAULTS["tags"],
            "dynamic": "", "tid": int(settings.get("tid") or DEFAULTS["tid"]),
            "status": "idle", "progress": 0, "message": "", "bvid": "", "aid": "", "error": ""}


def load_job_meta(path: Path, settings: dict) -> dict:
    meta = empty_meta(settings)
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            meta.update({key: saved[key] for key in meta if key in saved})
    with _upload_lock:
        live = _uploads.get(path.stem.removesuffix("-bilibili"))
    if live:
        meta.update(live)
    return meta


def save_job_meta(path: Path, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _set_upload(job_id: str, **fields) -> None:
    with _upload_lock:
        state = _uploads.setdefault(job_id, {})
        state.update(fields)


def _remember_upload(job_id: str, meta_path: Path, **fields) -> None:
    _set_upload(job_id, **fields)
    saved = {}
    if meta_path.is_file():
        try:
            saved = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved = {}
    if not isinstance(saved, dict):
        saved = {}
    saved.update(fields)
    save_job_meta(meta_path, saved)


def publish_state(job_id: str) -> dict | None:
    with _upload_lock:
        state = _uploads.get(job_id)
        return dict(state) if state else None


def _upload_file(session: requests.Session, video_path: Path, progress) -> dict:
    filename = video_path.name
    filesize = video_path.stat().st_size
    progress(5, "申请上传通道")
    pre = session.get("https://member.bilibili.com/preupload", params={
        "name": filename, "size": filesize, "r": "upos", "profile": "ugcupos/bup",
        "ssl": "0", "version": "2.14.0", "build": "2140000", "webVersion": "2.14.0",
    }, timeout=30)
    pre.raise_for_status()
    info = pre.json()
    if "upos_uri" not in info:
        raise RuntimeError(f"预上传失败：{info}")
    endpoints = []
    for candidate in [info.get("endpoint"), *(info.get("endpoints") or [])]:
        if not candidate:
            continue
        endpoint = str(candidate)
        if endpoint.startswith("//"):
            endpoint = "https:" + endpoint
        elif not endpoint.startswith("http"):
            endpoint = "https://" + endpoint.lstrip("/")
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    endpoint = endpoints[0] if endpoints else "https://upos-sz-upcdnbda2.bilivideo.com"
    object_key = info["upos_uri"].replace("upos://", "")
    auth = info["auth"]
    biz_id = info["biz_id"]
    chunk_size = int(info.get("chunk_size") or 4 * 1024 * 1024)
    headers = {"X-Upos-Auth": auth}
    init = session.post(f"{endpoint}/{object_key}", params={"uploads": "", "output": "json"},
                        headers=headers, timeout=30)
    init.raise_for_status()
    upload_id = init.json()["upload_id"]
    total = max(1, math.ceil(filesize / chunk_size))
    put_query = dict(parse_qsl(str(info.get("put_query") or ""), keep_blank_values=True))
    parts = []
    with video_path.open("rb") as handle:
        for index in range(total):
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            start = index * chunk_size
            params = {**put_query, "partNumber": index + 1, "uploadId": upload_id, "chunk": index,
                      "chunks": total, "size": len(chunk), "start": start, "end": start + len(chunk),
                      "total": filesize}
            last_error = "未知错误"
            for attempt in range(3):
                try:
                    response = session.put(f"{endpoint}/{object_key}", params=params, data=chunk,
                                           headers=headers, timeout=240)
                    if response.status_code == 200:
                        break
                    last_error = f"HTTP {response.status_code}"
                except requests.RequestException as exc:
                    last_error = str(exc) or type(exc).__name__
                time.sleep(1.5 * (attempt + 1))
            else:
                raise RuntimeError(f"分片 {index + 1}/{total} 上传失败：{last_error}")
            parts.append({"partNumber": index + 1, "eTag": "etag"})
            progress(5 + 85 * (index + 1) / total, f"上传中 {index + 1}/{total}")
    progress(92, "合并分片")
    finish = session.post(f"{endpoint}/{object_key}", params={
        "output": "json", "name": filename, "profile": "ugcupos/bup",
        "uploadId": upload_id, "biz_id": biz_id,
    }, json={"parts": parts}, headers=headers, timeout=60)
    finish.raise_for_status()
    return {"filename": Path(object_key).stem, "cid": biz_id, "title": video_path.stem[:80], "desc": ""}


def upload_cover(session: requests.Session, cover_path: Path, csrf: str) -> str:
    mime, _encoding = mimetypes.guess_type(cover_path.name)
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    encoded = base64.b64encode(cover_path.read_bytes()).decode("ascii")
    response = session.post(
        "https://member.bilibili.com/x/vu/web/cover/up",
        params={"ts": int(time.time() * 1000)},
        data={"csrf": csrf, "cover": f"data:{mime};base64,{encoded}"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"封面上传失败（{payload.get('code')}）：{payload.get('message') or payload}")
    url = str((payload.get("data") or {}).get("url") or "")
    if url.startswith("https:"):
        url = url[6:]
    elif url.startswith("http:"):
        url = url[5:]
    if not url:
        raise RuntimeError("封面上传未返回地址")
    return url


def _submit(session: requests.Session, cookies: dict, meta: dict, video: dict, cover_url: str = "") -> dict:
    csrf = cookies.get("bili_jct")
    if not csrf:
        raise RuntimeError("缺少 bili_jct，请重新登录")
    body = {
        "videos": [{"filename": video["filename"], "title": meta["title"][:80], "desc": "", "cid": video["cid"]}],
        "cover": cover_url, "cover43": "", "title": meta["title"][:80], "copyright": 1,
        "tid": int(meta["tid"]), "tag": tag_text(meta["tags"]) or "配音", "desc_format_id": 9999,
        "desc": meta["desc"][:2000], "recreate": -1, "dynamic": (meta.get("dynamic") or "")[:100],
        "interactive": 0, "act_reserve_create": 0, "no_disturbance": 0, "no_reprint": 0,
        "subtitle": {"open": 0, "lan": ""}, "dolby": 0, "lossless_music": 0,
        "up_selection_reply": False, "up_close_reply": False, "up_close_danmu": False,
        "web_os": 3, "open_elec": 0,
    }
    response = session.post("https://member.bilibili.com/x/vu/web/add/v3",
                            params={"ts": int(time.time() * 1000), "csrf": csrf},
                            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                            headers={"Content-Type": "application/json;charset=UTF-8"}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"提交稿件失败（{payload.get('code')}）：{payload.get('message') or payload}")
    return payload.get("data") or {}


def start_publish(job_id: str, video_path: Path, meta_path: Path, cookie_path: Path, meta: dict,
                  cover_path: Path | None = None, cover_loader=None) -> None:
    with _upload_lock:
        current = _uploads.get(job_id) or {}
        if current.get("status") == "uploading":
            raise RuntimeError("投稿正在进行")
        _uploads[job_id] = {"status": "uploading", "progress": 0, "message": "准备投稿", "error": ""}
    _remember_upload(job_id, meta_path, status="uploading", progress=0, message="准备投稿", error="")

    def run() -> None:
        def progress(value: float, message: str) -> None:
            _remember_upload(job_id, meta_path, status="uploading", progress=round(value, 1),
                             message=message, error="")

        try:
            cookies = load_cookies(cookie_path)
            if not cookies.get("SESSDATA"):
                raise RuntimeError("未登录 B 站，请先在设置中保存 Cookie")
            session = _session(cookies)
            cover_file = cover_path if cover_path and cover_path.is_file() else None
            if cover_loader and cover_file is None:
                progress(2, "下载视频封面")
                try:
                    cover_file = cover_loader()
                except Exception:
                    cover_file = None
            video = _upload_file(session, video_path, progress)
            cover_url = ""
            if cover_file and Path(cover_file).is_file():
                progress(93, "上传封面")
                cover_url = upload_cover(session, Path(cover_file), cookies["bili_jct"])
            progress(96, "提交稿件")
            result = _submit(session, cookies, meta, video, cover_url)
            fields = {"status": "success", "progress": 100, "message": "投稿成功",
                      "bvid": result.get("bvid") or "", "aid": result.get("aid") or "", "error": ""}
        except Exception as exc:
            fields = {"status": "error", "message": "投稿失败", "error": str(exc) or "投稿失败"}
        _remember_upload(job_id, meta_path, **fields)

    threading.Thread(target=run, name=f"bilibili-{job_id[:8]}", daemon=True).start()
