"""Local VidLingua web UI. Start with: python -m uvicorn web_app:app --host 127.0.0.1 --port 8000"""

from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from import_media import import_media
from azure_dubbing import (DEFAULTS as AZURE_DEFAULTS, VOICES as AZURE_VOICES,
                           assemble_elastic_voice_track, chinese_voices as azure_chinese_voices,
                           delete_key as delete_azure_key, get_key as get_azure_key,
                           get_keys as get_azure_keys, set_keys as set_azure_keys,
                           load_config as load_azure_config, make_voice_track,
                           plan_elastic_timeline, render_video,
                           save_config as save_azure_config,
                           validate_config as validate_azure_config,
                           validate_key as validate_azure_key,
                           voice_list as azure_voice_list, write_wav)
from deepseek_translation import (DEFAULTS, MODELS, delete_api_key, get_api_key, load_config,
                                  make_srt, make_bilingual_srt, save_config, set_api_key, translate_group, call_deepseek,
                                  validate_api_key, glossary_candidates, refine_glossary,
                                  GLOSSARY_VERSION, TRANSLATION_VERSION)
from prepare_translation import make_segments, realign_edits, term_candidates, translation_input
from watermark import (DEFAULTS as WATERMARK_DEFAULTS, load_config as load_watermark_config,
                       save_config as save_watermark_config)
from subtitle_burn import (DEFAULTS as SUBTITLE_DEFAULTS, load_config as load_subtitle_config,
                           save_config as save_subtitle_config)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / ".web_data"
UPLOADS = DATA / "uploads"
DB = DATA / "jobs.sqlite3"
DEEPSEEK_CONFIG = DATA / "deepseek_config.json"
AZURE_CONFIG = DATA / "azure_config.json"
WATERMARK_CONFIG = DATA / "watermark_config.json"
SUBTITLE_CONFIG = DATA / "subtitle_config.json"
WEB = ROOT / "web"
DATA.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vidlingua-gpu")
db_lock = threading.RLock()
app = FastAPI(title="VidLingua", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=WEB), name="static")


@contextmanager
def connect():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    with connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, subject TEXT NOT NULL,
            glossary TEXT NOT NULL, filename TEXT NOT NULL, upload_path TEXT NOT NULL,
            run_path TEXT, status TEXT NOT NULL, stage TEXT NOT NULL, error TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS segments (
            job_id TEXT NOT NULL, id TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
            raw_text TEXT NOT NULL, edited_text TEXT NOT NULL, words TEXT NOT NULL,
            flags TEXT NOT NULL, reviewed INTEGER NOT NULL, needs_realign INTEGER NOT NULL,
            PRIMARY KEY(job_id,id))""")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, definition in {
            "translation_status": "TEXT NOT NULL DEFAULT 'not_started'",
            "translation_progress": "INTEGER NOT NULL DEFAULT 0",
            "translation_total": "INTEGER NOT NULL DEFAULT 0",
            "translation_error": "TEXT NOT NULL DEFAULT ''",
            "dubbing_status": "TEXT NOT NULL DEFAULT 'not_started'",
            "dubbing_progress": "INTEGER NOT NULL DEFAULT 0",
            "dubbing_total": "INTEGER NOT NULL DEFAULT 0",
            "dubbing_error": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        conn.execute("UPDATE jobs SET status='interrupted', error='服务重启中断了处理；可点击重试' WHERE status IN ('queued','running')")
        conn.execute("UPDATE jobs SET translation_status='interrupted', translation_error='服务重启中断了翻译；可重试' WHERE translation_status IN ('queued','glossary','running')")
        conn.execute("UPDATE jobs SET dubbing_status='interrupted', dubbing_error='服务重启中断了配音；可重试' WHERE dubbing_status IN ('queued','running')")
        conn.execute("UPDATE jobs SET translation_error=? WHERE translation_error LIKE '%latin-1%codec%'",
                     ("已保存的 DeepSeek API Key 含非 ASCII 字符，请在左侧重新粘贴平台生成的密钥后重试",))


initialize()


def now():
    return datetime.now(timezone.utc).isoformat()


def get_job(job_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    job = dict(row)
    job["media"] = None
    if job["run_path"]:
        manifest = Path(job["run_path"]) / "manifest.json"
        if manifest.is_file():
            job["media"] = json.loads(manifest.read_text(encoding="utf-8"))["media"]
    return job


def update_job(job_id: str, **fields):
    fields["updated_at"] = now()
    with db_lock, connect() as conn:
        conn.execute("UPDATE jobs SET " + ", ".join(f"{key}=?" for key in fields)
                     + " WHERE id=?", (*fields.values(), job_id))


def segment_rows(job_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM segments WHERE job_id=? ORDER BY start,id", (job_id,)).fetchall()
    return [{**dict(row), "words": json.loads(row["words"]), "flags": json.loads(row["flags"]),
             "reviewed": bool(row["reviewed"]), "needs_realign": bool(row["needs_realign"])} for row in rows]


def save_segments(job_id: str, segments: list[dict]):
    with db_lock, connect() as conn:
        conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
        conn.executemany("""INSERT INTO segments
            (job_id,id,start,end,raw_text,edited_text,words,flags,reviewed,needs_realign)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", [
            (job_id, s["id"], s["start"], s["end"], s["raw_text"], s["edited_text"],
             json.dumps(s["words"]), json.dumps(s["flags"]), int(s["reviewed"]),
             int(s["needs_realign"])) for s in segments])


def invalidate_translation(job_id: str):
    invalidate_dubbing(job_id)
    for suffix in ("translation-input.json", "glossary.json", "translation.json", "zh.srt", "bilingual.srt"):
        (DATA / f"{job_id}-{suffix}").unlink(missing_ok=True)
    update_job(job_id, translation_status="not_started", translation_progress=0,
               translation_total=0, translation_error="")


def invalidate_dubbing(job_id: str):
    for suffix in ("voice.wav", "dubbed.mp4", "dubbing-report.json", "dubbed-zh.srt", "dubbed-bilingual.srt"):
        (DATA / f"{job_id}-{suffix}").unlink(missing_ok=True)
    update_job(job_id, dubbing_status="not_started", dubbing_progress=0,
               dubbing_total=0, dubbing_error="")


def translate_job(job_id: str):
    try:
        key = get_api_key()
        if not key:
            raise RuntimeError("尚未配置 DeepSeek API Key")
        packet_path = DATA / f"{job_id}-translation-input.json"
        packet_bytes = packet_path.read_bytes()
        packet = json.loads(packet_bytes)
        config = load_config(DEEPSEEK_CONFIG)
        digest = hashlib.sha256(packet_bytes).hexdigest()
        glossary_path = DATA / f"{job_id}-glossary.json"
        glossary = None
        if glossary_path.is_file():
            candidate = json.loads(glossary_path.read_text(encoding="utf-8"))
            if (candidate.get("source_sha256") == digest and candidate.get("model") == config["model"]
                    and candidate.get("glossary_version") == GLOSSARY_VERSION):
                glossary = candidate
        if glossary is None:
            update_job(job_id, translation_status="glossary", translation_progress=0,
                       translation_total=len(packet["groups"]), translation_error="")
            candidates = glossary_candidates(packet)
            subject, auto_terms, usage = refine_glossary(key, config, packet, candidates)
            merged = [f"{item['en']} = {item['zh']}" for item in auto_terms]
            glossary = {"glossary_version": GLOSSARY_VERSION, "source_sha256": digest,
                        "model": config["model"], "subject": subject,
                        "candidates": candidates, "auto_terms": auto_terms,
                        "merged_terms": merged, "usage": usage}
            glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        packet["glossary"] = glossary["merged_terms"]
        packet["subject"] = glossary["subject"]
        glossary_digest = hashlib.sha256(json.dumps(
            {"subject": packet["subject"], "terms": glossary["merged_terms"]},
            ensure_ascii=False).encode("utf-8")).hexdigest()
        output_path = DATA / f"{job_id}-translation.json"
        output = {"schema_version": 1, "job_id": job_id, "model": config["model"],
                  "translation_version": TRANSLATION_VERSION,
                  "source_sha256": digest, "glossary_sha256": glossary_digest,
                  "glossary": glossary["merged_terms"], "groups": [], "usage": {}}
        if output_path.is_file():
            previous = json.loads(output_path.read_text(encoding="utf-8"))
            if (previous.get("source_sha256") == digest and previous.get("model") == config["model"]
                    and previous.get("glossary_sha256") == glossary_digest
                    and previous.get("translation_version") == TRANSLATION_VERSION):
                output = previous
        completed = {group["id"] for group in output["groups"]}
        update_job(job_id, translation_status="running", translation_progress=len(completed),
                   translation_total=len(packet["groups"]), translation_error="")
        for group in packet["groups"]:
            if group["id"] in completed:
                continue
            translated, usage = translate_group(key, config, packet, group)
            output["groups"].append({"id": group["id"], "segments": translated, "usage": usage})
            for key_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                output["usage"][key_name] = output["usage"].get(key_name, 0) + usage.get(key_name, 0)
            output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            update_job(job_id, translation_progress=len(output["groups"]))
        indexed = {group["id"]: group for group in output["groups"]}
        output["groups"] = [indexed[group["id"]] for group in packet["groups"]]
        items = [item for group in output["groups"] for item in group["segments"]]
        (DATA / f"{job_id}-zh.srt").write_text(make_srt(items), encoding="utf-8-sig")
        (DATA / f"{job_id}-bilingual.srt").write_text(make_bilingual_srt(items), encoding="utf-8-sig")
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        update_job(job_id, translation_status="done", translation_error="")
    except Exception as exc:
        update_job(job_id, translation_status="failed", translation_error=str(exc))


def translate_then_dub(job_id: str):
    try:
        job = get_job(job_id)
        if job["status"] != "prepared" or not (DATA / f"{job_id}-translation-input.json").is_file():
            prepare(job_id)
        translate_job(job_id)
        if get_job(job_id)["translation_status"] == "done":
            dub_job(job_id)
    except Exception as exc:
        update_job(job_id, translation_status="failed", translation_error=str(exc))


def process(job_id: str):
    try:
        job = get_job(job_id)
        log = DATA / f"{job_id}.log"
        log.write_text("", encoding="utf-8")
        update_job(job_id, status="running", stage="import", error="")
        run = import_media(Path(job["upload_path"]), ROOT / "runs")
        update_job(job_id, run_path=str(run), stage="separate")
        with log.open("a", encoding="utf-8") as output:
            result = subprocess.run([sys.executable, str(ROOT / "separate_audio.py"), str(run)],
                                    cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            raise RuntimeError(f"人声分离失败，退出码 {result.returncode}；请查看处理日志")
        update_job(job_id, stage="transcribe")
        transcript = run / "transcript_vocals" / "asr_vocals.json"
        if not transcript.is_file():
            with log.open("a", encoding="utf-8") as output:
                result = subprocess.run([sys.executable, str(ROOT / "transcribe_demo.py"),
                                         str(run), "--audio-source", "vocals"],
                                        cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                        check=False)
            if result.returncode or not transcript.is_file():
                raise RuntimeError(f"WhisperX 失败，退出码 {result.returncode}；请查看处理日志")
        update_job(job_id, stage="organize")
        duration = json.loads((run / "manifest.json").read_text(encoding="utf-8"))["media"]["duration_seconds"]
        if not segment_rows(job_id):
            save_segments(job_id, make_segments(transcript, duration))
        update_job(job_id, stage="review", status="ready")
        translate_then_dub(job_id)
    except Exception as exc:
        update_job(job_id, status="failed", error=str(exc))


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/jobs")
def list_jobs():
    with connect() as conn:
        rows = conn.execute("""SELECT id,title,filename,status,stage,error,created_at,
                               translation_status,dubbing_status
                               FROM jobs ORDER BY created_at DESC""").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/jobs")
def create_job(file: UploadFile = File(...), title: str = Form("")):
    filename = Path(file.filename or "video.mp4").name
    if Path(filename).suffix.lower() != ".mp4":
        raise HTTPException(400, "首版仅支持 MP4 视频")
    job_id = uuid.uuid4().hex[:12]
    upload_dir = UPLOADS / job_id
    upload_dir.mkdir()
    upload = upload_dir / filename
    with upload.open("wb") as target:
        shutil.copyfileobj(file.file, target, length=1024 * 1024)
    if upload.stat().st_size == 0:
        upload.unlink()
        raise HTTPException(400, "文件为空")
    timestamp = now()
    with db_lock, connect() as conn:
        conn.execute("""INSERT INTO jobs
            (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (job_id, title.strip() or Path(filename).stem, "", "",
                      filename, str(upload), None, "created", "upload", "", timestamp, timestamp))
    return get_job(job_id)


@app.post("/api/jobs/{job_id}/run")
def run_job(job_id: str):
    job = get_job(job_id)
    if job["status"] in ("queued", "running"):
        raise HTTPException(409, "任务正在处理")
    update_job(job_id, status="queued", error="")
    executor.submit(process, job_id)
    return get_job(job_id)


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    job = get_job(job_id)
    segments = segment_rows(job_id)
    job["segment_count"] = len(segments)
    job["review_count"] = sum(bool(s["flags"]) and not s["reviewed"] for s in segments)
    job["term_candidates"] = term_candidates(segments)
    return job


@app.get("/api/jobs/{job_id}/segments")
def get_segments(job_id: str):
    get_job(job_id)
    return segment_rows(job_id)


class SegmentEdit(BaseModel):
    edited_text: str | None = None
    reviewed: bool | None = None


@app.patch("/api/jobs/{job_id}/segments/{segment_id}")
def edit_segment(job_id: str, segment_id: str, edit: SegmentEdit):
    job = get_job(job_id)
    if job["translation_status"] in ("queued", "glossary", "running"):
        raise HTTPException(409, "翻译进行中，完成后再修订英文")
    with db_lock, connect() as conn:
        row = conn.execute("SELECT * FROM segments WHERE job_id=? AND id=?", (job_id, segment_id)).fetchone()
        if row is None:
            raise HTTPException(404, "句段不存在")
        text = row["edited_text"] if edit.edited_text is None else " ".join(edit.edited_text.split())
        if not text:
            raise HTTPException(400, "句段文本不能为空")
        changed = text != row["edited_text"]
        reviewed = bool(row["reviewed"]) if edit.reviewed is None else edit.reviewed
        conn.execute("""UPDATE segments SET edited_text=?, reviewed=?, needs_realign=?
                        WHERE job_id=? AND id=?""",
                     (text, int(reviewed), int(bool(row["needs_realign"]) or changed), job_id, segment_id))
    update_job(job_id, stage="review", status="ready")
    invalidate_translation(job_id)
    return {"ok": True}


class JobEdit(BaseModel):
    title: str | None = None
    subject: str | None = None
    glossary: str | None = None


@app.patch("/api/jobs/{job_id}")
def edit_job(job_id: str, edit: JobEdit):
    job = get_job(job_id)
    if job["translation_status"] in ("queued", "glossary", "running"):
        raise HTTPException(409, "翻译进行中，完成后再修改课程背景")
    fields = edit.model_dump(exclude_none=True)
    if fields:
        update_job(job_id, **{key: value.strip() for key, value in fields.items()})
        invalidate_translation(job_id)
        update_job(job_id, stage="review", status="ready")
    return get_job(job_id)


@app.post("/api/jobs/{job_id}/prepare-translation")
def prepare(job_id: str):
    job = get_job(job_id)
    if job["translation_status"] in ("queued", "glossary", "running"):
        raise HTTPException(409, "翻译进行中")
    if job["status"] not in ("ready", "prepared") or not job["run_path"]:
        raise HTTPException(409, "请先完成转写")
    segments = segment_rows(job_id)
    if not segments:
        raise HTTPException(409, "没有可导出的句段")
    try:
        realign_edits(segments, Path(job["run_path"]) / "asr_vocals.wav")
    except Exception as exc:
        raise HTTPException(422, f"修订句段对齐失败：{exc}") from exc
    save_segments(job_id, segments)
    packet = translation_input(job, segments)
    packet["subject"] = ""
    packet["glossary"] = []
    path = DATA / f"{job_id}-translation-input.json"
    serialized = json.dumps(packet, ensure_ascii=False, indent=2) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") != serialized:
        invalidate_translation(job_id)
    path.write_text(serialized, encoding="utf-8")
    update_job(job_id, stage="prepared", status="prepared")
    return packet


@app.get("/api/jobs/{job_id}/translation-input.json")
def download_translation(job_id: str):
    get_job(job_id)
    path = DATA / f"{job_id}-translation-input.json"
    if not path.is_file():
        raise HTTPException(404, "请先准备译前包")
    return FileResponse(path, media_type="application/json", filename=f"{job_id}-translation-input.json")


@app.get("/api/jobs/{job_id}/media/{kind}")
def media(job_id: str, kind: str):
    job = get_job(job_id)
    if not job["run_path"]:
        raise HTTPException(404, "媒体尚未导入")
    run = Path(job["run_path"])
    names = {"video": "source.mp4", "vocals": "asr_vocals.wav",
             "instrumental": "separated/instrumental.flac"}
    if kind not in names:
        raise HTTPException(404)
    path = run / names[kind]
    if not path.is_file():
        raise HTTPException(404, "媒体尚未就绪")
    return FileResponse(path)


@app.get("/api/jobs/{job_id}/log")
def log(job_id: str):
    get_job(job_id)
    path = DATA / f"{job_id}.log"
    return {"text": path.read_text(encoding="utf-8", errors="replace")[-12000:] if path.is_file() else ""}


class DeepSeekConfigEdit(BaseModel):
    model: str = DEFAULTS["model"]
    temperature: float = DEFAULTS["temperature"]
    max_tokens: int = DEFAULTS["max_tokens"]
    api_key: str | None = None
    remove_key: bool = False


class WatermarkConfigEdit(BaseModel):
    enabled: bool = WATERMARK_DEFAULTS["enabled"]
    text: str = WATERMARK_DEFAULTS["text"]
    position: str = WATERMARK_DEFAULTS["position"]
    opacity: float = WATERMARK_DEFAULTS["opacity"]


@app.get("/api/watermark/config")
def watermark_config():
    return load_watermark_config(WATERMARK_CONFIG)


@app.put("/api/watermark/config")
def configure_watermark(edit: WatermarkConfigEdit):
    try:
        return save_watermark_config(WATERMARK_CONFIG, edit.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class SubtitleConfigEdit(BaseModel):
    enabled: bool = SUBTITLE_DEFAULTS["enabled"]
    mode: str = SUBTITLE_DEFAULTS["mode"]
    font_size: int = SUBTITLE_DEFAULTS["font_size"]
    margin_bottom: int = SUBTITLE_DEFAULTS["margin_bottom"]
    outline: int = SUBTITLE_DEFAULTS["outline"]


@app.get("/api/subtitles/config")
def subtitle_config():
    return load_subtitle_config(SUBTITLE_CONFIG)


@app.put("/api/subtitles/config")
def configure_subtitles(edit: SubtitleConfigEdit):
    try:
        return save_subtitle_config(SUBTITLE_CONFIG, edit.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/deepseek/config")
def deepseek_config():
    key = get_api_key()
    try:
        if key:
            validate_api_key(key)
        usable = bool(key)
    except ValueError:
        usable = False
    return {**load_config(DEEPSEEK_CONFIG), "api_key_configured": bool(key),
            "api_key_usable": usable,
            "base_url": "https://api.deepseek.com", "models": sorted(MODELS)}


@app.put("/api/deepseek/config")
def configure_deepseek(edit: DeepSeekConfigEdit):
    config = edit.model_dump(include={"model", "temperature", "max_tokens"})
    try:
        if edit.api_key:
            validate_api_key(edit.api_key)
        save_config(DEEPSEEK_CONFIG, config)
        if edit.api_key:
            set_api_key(edit.api_key)
        elif edit.remove_key:
            delete_api_key()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return deepseek_config()


@app.post("/api/deepseek/test")
def test_deepseek():
    key = get_api_key()
    if not key:
        raise HTTPException(409, "请先配置 API Key")
    try:
        validate_api_key(key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        result = call_deepseek(key, load_config(DEEPSEEK_CONFIG), [
            {"role": "system", "content": "Return JSON only, exactly {\"ok\":true}."},
            {"role": "user", "content": "Connection test. Return JSON."},
        ])
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    content = result["content"]
    return {"ok": isinstance(content, dict) and content.get("ok") is True,
            "model": load_config(DEEPSEEK_CONFIG)["model"]}


@app.post("/api/jobs/{job_id}/translate")
def start_translation(job_id: str, force: bool = False):
    job = get_job(job_id)
    if job["status"] not in ("ready", "prepared"):
        raise HTTPException(409, "请先完成英文转写")
    key = get_api_key()
    if not key:
        raise HTTPException(409, "请先配置 DeepSeek API Key")
    try:
        validate_api_key(key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if job["translation_status"] in ("queued", "glossary", "running"):
        raise HTTPException(409, "翻译正在进行")
    if job["translation_status"] == "done":
        if not force:
            raise HTTPException(409, "翻译已完成；如需重新分析课程并翻译，请明确选择重新翻译")
        invalidate_dubbing(job_id)
        for suffix in ("glossary.json", "translation.json", "zh.srt", "bilingual.srt"):
            existing = DATA / f"{job_id}-{suffix}"
            if existing.is_file():
                shutil.copy2(existing, DATA / f"{job_id}-previous-{suffix}")
                existing.unlink()
        update_job(job_id, translation_progress=0, translation_total=0)
    update_job(job_id, translation_status="queued", translation_error="")
    executor.submit(translate_then_dub, job_id)
    return get_job(job_id)


@app.get("/api/jobs/{job_id}/translation.json")
def translation_result(job_id: str):
    get_job(job_id)
    path = DATA / f"{job_id}-translation.json"
    if not path.is_file():
        raise HTTPException(404, "尚无翻译结果")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/jobs/{job_id}/glossary.json")
def glossary_result(job_id: str):
    get_job(job_id)
    path = DATA / f"{job_id}-glossary.json"
    if not path.is_file():
        raise HTTPException(404, "术语表尚未生成")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/jobs/{job_id}/zh.srt")
def download_srt(job_id: str):
    get_job(job_id)
    path = DATA / f"{job_id}-zh.srt"
    if not path.is_file():
        raise HTTPException(404, "尚无中文字幕")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f"{job_id}-zh.srt")


@app.get("/api/jobs/{job_id}/bilingual.srt")
def download_bilingual_srt(job_id: str):
    job = get_job(job_id)
    if job["translation_status"] != "done":
        raise HTTPException(404, "尚无双语字幕")
    path = DATA / f"{job_id}-bilingual.srt"
    if not path.is_file():
        result = translation_result(job_id)
        items = [item for group in result["groups"] for item in group["segments"]]
        path.write_text(make_bilingual_srt(items), encoding="utf-8-sig")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f"{job_id}-bilingual.srt")


class AzureConfigEdit(BaseModel):
    region: str = AZURE_DEFAULTS["region"]
    voice: str = AZURE_DEFAULTS["voice"]
    per_key_concurrency: int = AZURE_DEFAULTS["per_key_concurrency"]
    max_synthesis_adjust: float = AZURE_DEFAULTS["max_synthesis_adjust"]
    max_post_adjust: float = AZURE_DEFAULTS["max_post_adjust"]
    max_global_adjust: float = AZURE_DEFAULTS["max_global_adjust"]
    max_local_adjust: float = AZURE_DEFAULTS["max_local_adjust"]
    max_video_adjust: float = AZURE_DEFAULTS["max_video_adjust"]
    background_volume: float = AZURE_DEFAULTS["background_volume"]
    api_keys: str | None = None
    api_key: str | None = None
    remove_key: bool = False


@app.get("/api/azure/config")
def azure_config():
    keys = get_azure_keys()
    return {**load_azure_config(AZURE_CONFIG), "api_key_configured": bool(keys),
            "key_count": len(keys),
            "voices": AZURE_VOICES}


@app.put("/api/azure/config")
def configure_azure(edit: AzureConfigEdit):
    config = edit.model_dump(include=set(AZURE_DEFAULTS))
    try:
        config = validate_azure_config(config)
        new_keys = edit.api_keys or edit.api_key
        if new_keys:
            parsed = [line.strip() for line in new_keys.splitlines() if line.strip()]
            for item in parsed:
                validate_azure_key(item)
            if not 1 <= len(set(parsed)) <= 16:
                raise ValueError("请填写 1–16 把 Azure Speech 密钥")
        save_azure_config(AZURE_CONFIG, config)
        if new_keys:
            set_azure_keys(parsed)
        elif edit.remove_key:
            delete_azure_key()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return azure_config()


@app.get("/api/azure/voices")
@app.post("/api/azure/test")
def test_azure():
    key = get_azure_key()
    config = load_azure_config(AZURE_CONFIG)
    if not key or not config["region"]:
        raise HTTPException(409, "请先填写 Azure Speech 密钥和区域")
    try:
        voices = azure_voice_list(key, config["region"])
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    chinese = azure_chinese_voices(voices)
    return {"ok": any(voice["name"] == config["voice"] for voice in chinese),
            "voice": config["voice"], "voice_count": len(chinese), "voices": chinese,
            "region": config["region"]}


def dub_job(job_id: str):
    try:
        job = get_job(job_id)
        keys = get_azure_keys()
        config = validate_azure_config(load_azure_config(AZURE_CONFIG))
        watermark_settings = load_watermark_config(WATERMARK_CONFIG)
        subtitle_settings = load_subtitle_config(SUBTITLE_CONFIG)
        if not keys or not config["region"]:
            raise RuntimeError("尚未配置 Azure Speech 密钥和区域")
        for key in keys:
            validate_azure_key(key)
        result = translation_result(job_id)
        segments = [item for group in result["groups"] for item in group["segments"]]
        if not segments:
            raise RuntimeError("没有可配音的中文句段")
        run = Path(job["run_path"])
        duration = float(job["media"]["duration_seconds"])
        update_job(job_id, dubbing_status="running", dubbing_progress=0,
                   dubbing_total=len(segments), dubbing_error="")
        baseline = {}
        cache_dir = DATA / "dubbing" / job_id
        source_track, _, timings = make_voice_track(
            segments, config, keys, cache_dir, duration,
            progress=lambda count: update_job(job_id, dubbing_progress=count),
            baseline_info=baseline)
        pieces, cues, retiming, issues, output_duration = plan_elastic_timeline(
            segments, timings, duration, max_adjust=float(config["max_video_adjust"]))
        track = (source_track if issues else assemble_elastic_voice_track(
            segments, timings, config, cache_dir, output_duration))
        voice_path = DATA / f"{job_id}-voice.wav"
        write_wav(voice_path, track)
        affected_segments = len({item.get("id") or item.get("kind") for item in issues})
        report = {"job_id": job_id, "voice": config["voice"], "region": config["region"],
                  "segments": len(segments), "issues": issues,
                  "azure_key_count": len(keys),
                  "per_key_concurrency": config["per_key_concurrency"],
                  "affected_segments": affected_segments, "timings": timings,
                  "baseline": baseline,
                  "source_duration_seconds": round(duration, 3),
                  "output_duration_seconds": round(output_duration, 3),
                  "watermark": watermark_settings,
                  "subtitles": subtitle_settings,
                  "retiming": retiming,
                  "policy": {"max_synthesis_adjust": config["max_synthesis_adjust"],
                             "max_post_adjust": config["max_post_adjust"],
                             "max_global_adjust": config["max_global_adjust"],
                             "max_local_adjust": config["max_local_adjust"],
                             "max_video_adjust": config["max_video_adjust"],
                             "video_retiming": "按句段延长画面与背景音，后续配音和字幕同步顺延"}}
        (DATA / f"{job_id}-dubbing-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if issues:
            update_job(job_id, dubbing_status="needs_review", dubbing_error=f"{affected_segments} 句存在时长问题，请下载报告并调整中文台词")
            return
        (DATA / f"{job_id}-dubbed-zh.srt").write_text(make_srt(cues), encoding="utf-8-sig")
        (DATA / f"{job_id}-dubbed-bilingual.srt").write_text(make_bilingual_srt(cues), encoding="utf-8-sig")
        burn_path = DATA / f"{job_id}-dubbed-{'bilingual' if subtitle_settings['mode'] == 'bilingual' else 'zh'}.srt"
        render_video(run / "source.mp4", run / "separated" / "instrumental.flac",
                     voice_path, DATA / f"{job_id}-dubbed.mp4", output_duration,
                     float(config["background_volume"]),
                     pieces if retiming else None,
                     watermark=watermark_settings,
                     subtitles=subtitle_settings, subtitle_path=burn_path)
        update_job(job_id, dubbing_status="done", dubbing_error="")
    except Exception as exc:
        update_job(job_id, dubbing_status="failed", dubbing_error=str(exc))


@app.post("/api/jobs/{job_id}/dub")
def start_dubbing(job_id: str):
    job = get_job(job_id)
    if job["translation_status"] != "done":
        raise HTTPException(409, "请先完成中文翻译")
    if job["dubbing_status"] in ("queued", "running"):
        raise HTTPException(409, "配音正在进行")
    keys = get_azure_keys()
    config = load_azure_config(AZURE_CONFIG)
    if not keys or not config["region"]:
        raise HTTPException(409, "请先配置 Azure Speech 密钥和区域")
    try:
        for key in keys:
            validate_azure_key(key)
        validate_azure_config(config)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    invalidate_dubbing(job_id)
    update_job(job_id, dubbing_status="queued")
    executor.submit(dub_job, job_id)
    return get_job(job_id)


@app.get("/api/jobs/{job_id}/voice.wav")
def download_voice(job_id: str):
    get_job(job_id)
    path = DATA / f"{job_id}-voice.wav"
    if not path.is_file():
        raise HTTPException(404, "尚无中文配音轨")
    return FileResponse(path, media_type="audio/wav", filename=f"{job_id}-voice.wav")


@app.get("/api/jobs/{job_id}/dubbed.mp4")
def download_dubbed_video(job_id: str):
    job = get_job(job_id)
    path = DATA / f"{job_id}-dubbed.mp4"
    if job["dubbing_status"] != "done" or not path.is_file():
        raise HTTPException(404, "配音视频尚未就绪")
    return FileResponse(path, media_type="video/mp4", filename=f"{job_id}-dubbed.mp4")


@app.get("/api/jobs/{job_id}/dubbed-zh.srt")
def download_dubbed_srt(job_id: str):
    job = get_job(job_id)
    path = DATA / f"{job_id}-dubbed-zh.srt"
    if job["dubbing_status"] != "done" or not path.is_file():
        raise HTTPException(404, "配音字幕尚未就绪")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f"{job_id}-dubbed-zh.srt")


@app.get("/api/jobs/{job_id}/dubbed-bilingual.srt")
def download_dubbed_bilingual_srt(job_id: str):
    job = get_job(job_id)
    path = DATA / f"{job_id}-dubbed-bilingual.srt"
    if job["dubbing_status"] != "done" or not path.is_file():
        raise HTTPException(404, "配音双语字幕尚未就绪")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f"{job_id}-dubbed-bilingual.srt")


@app.get("/api/jobs/{job_id}/dubbing-report.json")
def dubbing_report(job_id: str):
    get_job(job_id)
    path = DATA / f"{job_id}-dubbing-report.json"
    if not path.is_file():
        raise HTTPException(404, "配音报告尚未生成")
    return json.loads(path.read_text(encoding="utf-8"))


class ChineseEdit(BaseModel):
    zh: str


@app.patch("/api/jobs/{job_id}/translation/{segment_id}")
def edit_chinese(job_id: str, segment_id: str, edit: ChineseEdit):
    job = get_job(job_id)
    if job["translation_status"] != "done" or job["dubbing_status"] in ("queued", "running"):
        raise HTTPException(409, "请在翻译完成且配音未运行时修改中文")
    chinese = " ".join(edit.zh.split())
    if not chinese or len(chinese) > 500:
        raise HTTPException(400, "中文台词须为 1–500 字")
    path = DATA / f"{job_id}-translation.json"
    with db_lock:
        result = json.loads(path.read_text(encoding="utf-8"))
        items = [item for group in result["groups"] for item in group["segments"]]
        target = next((item for item in items if item["id"] == segment_id), None)
        if target is None:
            raise HTTPException(404, "译文句段不存在")
        target["zh"] = chinese
        target["user_edited"] = True
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (DATA / f"{job_id}-zh.srt").write_text(make_srt(items), encoding="utf-8-sig")
        (DATA / f"{job_id}-bilingual.srt").write_text(make_bilingual_srt(items), encoding="utf-8-sig")
        invalidate_dubbing(job_id)
    return {"ok": True, "id": segment_id, "zh": chinese}
