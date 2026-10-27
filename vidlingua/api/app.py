"""Local VidLingua web UI. Start with: python -m uvicorn web_app:app --host 127.0.0.1 --port 8000"""

from __future__ import annotations

import json
import hashlib
import copy
import io
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Literal

from ..pipeline.import_media import import_media
from ..pipeline.external_subtitles import matching_subtitle, import_subtitle_transcript
from ..pipeline.batch import VIDEO_SUFFIXES, prepare_video as prepare_batch_video, publish_video as publish_batch_video, scan_directory
from ..pipeline.youtube import (canonical_youtube_url, download_youtube,
                                load_config as load_youtube_config,
                                save_config as save_youtube_config,
                                save_cookies as save_youtube_cookies,
                                test_connection as test_youtube_connection,
                                validate_config as validate_youtube_config)
from ..providers.azure_speech import (DEFAULTS as AZURE_DEFAULTS, VOICES as AZURE_VOICES,
                           SAMPLE_RATE,
                           assemble_elastic_voice_track, chinese_voices as azure_chinese_voices,
                           clip_cache_key, read_wav, stretch,
                           delete_key as delete_azure_key, get_key as get_azure_key,
                           get_keys as get_azure_keys, set_keys as set_azure_keys,
                           load_config as load_azure_config, make_voice_track,
                           plan_elastic_timeline, render_video,
                           save_config as save_azure_config,
                           validate_config as validate_azure_config,
                           validate_key as validate_azure_key,
                           voice_list as azure_voice_list, write_wav)
from ..providers.doubao_speech import (DEFAULTS as DOUBAO_DEFAULTS, RESOURCE_IDS as DOUBAO_RESOURCE_IDS,
                                       VOICES as DOUBAO_VOICES, delete_key as delete_doubao_key,
                                       get_key as get_doubao_key, load_config as load_doubao_config,
                                       save_config as save_doubao_config, set_key as set_doubao_key,
                                       synthesize as doubao_synthesize,
                                       validate_config as validate_doubao_config,
                                       validate_key as validate_doubao_key)
from ..providers import bilibili as bilibili_provider
from ..providers.deepseek import (DEFAULTS, MODELS, delete_api_key, get_api_key, load_config,
                                  make_srt, make_bilingual_srt, make_english_srt, save_config, set_api_key,
                                  translate_group, correct_english_group, call_deepseek,
                                  validate_api_key, glossary_candidates, refine_glossary,
                                  GLOSSARY_VERSION, TRANSLATION_VERSION, CORRECTION_VERSION)
from ..pipeline.preparation import make_segments, realign_edits, term_candidates, translation_input
from ..rendering.watermark import (DEFAULTS as WATERMARK_DEFAULTS, load_config as load_watermark_config,
                       save_config as save_watermark_config)
from ..rendering.subtitles import (DEFAULTS as SUBTITLE_DEFAULTS, load_config as load_subtitle_config,
                           save_config as save_subtitle_config)
from ..rendering import manual_edit
from ..storage import jobs as job_store
from ..services import translation as translation_service, dubbing as dubbing_service
from . import settings as settings_service

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / ".web_data"
UPLOADS = DATA / "uploads"
DB = DATA / "jobs.sqlite3"
DEEPSEEK_CONFIG = DATA / "deepseek_config.json"
AZURE_CONFIG = DATA / "azure_config.json"
DOUBAO_CONFIG = DATA / "doubao_config.json"
WATERMARK_CONFIG = DATA / "watermark_config.json"
SUBTITLE_CONFIG = DATA / "subtitle_config.json"
YOUTUBE_CONFIG = DATA / "youtube_config.json"
YOUTUBE_COOKIES = DATA / "youtube_cookies.txt"
BILIBILI_CONFIG = DATA / "bilibili_config.json"
BILIBILI_COOKIES = DATA / "bilibili_cookies.json"
WEB = ROOT / "web"
DATA.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)
executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vidlingua-job")
heavy_media_lock = threading.Lock()
folder_picker_lock = threading.Lock()
db_lock = threading.RLock()


class PauseRequested(Exception):
    """A worker reached a safe pause point."""


def check_pause(job_id: str) -> None:
    with connect() as conn:
        row = conn.execute("SELECT pause_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["pause_requested"]:
        raise PauseRequested()


def mark_paused(job_id: str) -> None:
    job = get_job(job_id)
    fields = {"status": "paused"}
    if job["translation_status"] in ("queued", "correcting", "glossary", "running"):
        fields["translation_status"] = "paused"
    if job["dubbing_status"] in ("queued", "running"):
        fields["dubbing_status"] = "paused"
    update_job(job_id, **fields)


@contextmanager
def pause_aware_pool(max_workers: int):
    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        yield pool
    except PauseRequested:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)


@contextmanager
def heavy_media_stage(job_id: str, label: str, *, pause_sensitive: bool = True):
    """Run only one local high-load media stage at a time across jobs."""
    if not heavy_media_lock.acquire(blocking=False):
        with (DATA / f"{job_id}.log").open("a", encoding="utf-8") as log:
            log.write(f"等待重负载资源：{label}\n")
        while not heavy_media_lock.acquire(timeout=0.5):
            if pause_sensitive:
                check_pause(job_id)
    try:
        if pause_sensitive:
            check_pause(job_id)
        with (DATA / f"{job_id}.log").open("a", encoding="utf-8") as log:
            log.write(f"开始重负载阶段：{label}\n")
        yield
    finally:
        heavy_media_lock.release()
@asynccontextmanager
async def app_lifespan(application: FastAPI):
    initialize()
    yield


app = FastAPI(title="VidLingua", docs_url="/api/docs", lifespan=app_lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")


def connect():
    return job_store.connect(DB)


def initialize():
    job_store.initialize(connect)




def now():
    return datetime.now(timezone.utc).isoformat()


def get_job(job_id: str) -> dict:
    return job_store.get_job(connect, job_id)


def download_filename(job: dict, suffix: str) -> str:
    raw = str(job.get("title") or Path(str(job.get("filename") or "")).stem or "video")
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")
    return f"{(cleaned or 'video')[:80]}{suffix}"


def update_job(job_id: str, **fields):
    with db_lock:
        if fields.get("pause_requested") != 0 and (fields.get("status") in ("ready", "prepared") or
                fields.get("translation_status") == "done" or
                fields.get("dubbing_status") in ("done", "needs_review")):
            check_pause(job_id)
        return job_store.update_job(connect, db_lock, DATA, job_id, **fields)


def stage_progress(job: dict, job_id: str) -> dict:
    """Current-stage progress; null percent means the underlying tool has no reliable counter."""
    if job["status"] == "paused":
        return {"percent": None, "label": "已暂停；可点击继续任务"}
    if job.get("pause_requested"):
        return {"percent": None, "label": "正在等待当前步骤结束后暂停"}
    if job["dubbing_status"] in ("running", "done", "needs_review", "failed"):
        if job["stage"] == "publish" and job["dubbing_status"] == "running":
            return {"percent": None, "label": "正在将成片写入 Translate 文件夹"}
        if job["stage"] == "render" and job["dubbing_status"] == "running":
            return {"percent": None, "label": "正在合成视频；视频编码暂不提供准确百分比"}
        done, total = job["dubbing_progress"], job["dubbing_total"]
        if total:
            label = f"语音合成 {done}/{total} 句"
            if job["dubbing_status"] == "needs_review":
                label += "；时长待复核"
            elif job["dubbing_status"] == "running" and done == total:
                label += "；正在拼接和检查时长"
            return {"percent": round(100 * done / total, 1), "label": label}
    if job["translation_status"] in ("correcting", "running"):
        done, total = job["translation_progress"], job["translation_total"]
        stage_name = "英文校正" if job["translation_status"] == "correcting" else "字幕翻译"
        return {"percent": round(100 * done / total, 1) if total else None,
                "label": f"{stage_name} {done}/{total} 组"}
    if job["status"] == "running" and job["stage"] in ("download", "separate", "transcribe"):
        path = DATA / f"{job_id}.log"
        if path.is_file():
            with path.open("rb") as source:
                source.seek(max(0, path.stat().st_size - 131072))
                tail = source.read().decode("utf-8", errors="replace")
            marker = f"阶段：{job['stage']}"
            if marker in tail:
                tail = tail.rsplit(marker, 1)[-1]
            if job["stage"] == "download":
                matches = re.findall(r"YouTube:\s*(\d+)%", tail)
                if matches:
                    percent = float(matches[-1])
                    return {"percent": percent, "label": f"YouTube 下载 {percent:.0f}%"}
            elif job["stage"] == "separate":
                matches = re.findall(r"(\d+)/(\d+)\s*\[", tail)
                if matches:
                    done, total = map(int, matches[-1])
                    return {"percent": round(100 * done / total, 1) if total else None,
                            "label": f"人声分离 {done}/{total} 块"}
            else:
                matches = re.findall(r"Progress:\s*(\d+(?:\.\d+)?)%", tail)
                if matches:
                    percent = min(100.0, float(matches[-1]))
                    return {"percent": percent, "label": f"WhisperX 当前步骤 {percent:.1f}%"}
    return {"percent": None, "label": "当前步骤运行中；尚无可计算的内部进度"}


def segment_rows(job_id: str) -> list[dict]:
    return job_store.segment_rows(connect, job_id)


def save_segments(job_id: str, segments: list[dict]):
    return job_store.save_segments(connect, db_lock, job_id, segments)


def invalidate_translation(job_id: str):
    invalidate_dubbing(job_id)
    for suffix in ("translation-input.json", "corrected-english.json", "corrected-en.srt",
                   "glossary.json", "translation.json", "zh.srt", "bilingual.srt"):
        (DATA / f"{job_id}-{suffix}").unlink(missing_ok=True)
    update_job(job_id, translation_status="not_started", translation_progress=0,
               translation_total=0, translation_error="")


def invalidate_dubbing(job_id: str, preserve_report: bool = False):
    suffixes = ["voice.wav", "dubbed.mp4", "dubbed-zh.srt", "dubbed-bilingual.srt",
                "manual-edit.json", "edited.mp4", "edited-zh.srt", "edited-bilingual.srt"]
    if not preserve_report:
        suffixes.append("dubbing-report.json")
    for suffix in suffixes:
        (DATA / f"{job_id}-{suffix}").unlink(missing_ok=True)
    for thumbnail in DATA.glob(f"{job_id}-edit-thumb-*.jpg"):
        thumbnail.unlink(missing_ok=True)
    update_job(job_id, dubbing_status="not_started", dubbing_progress=0,
               dubbing_total=0, dubbing_error="")


def correct_packet(job_id: str, key: str, config: dict, packet: dict, source_digest: str) -> dict:
    return translation_service.correct_packet(sys.modules[__name__], job_id, key, config, packet, source_digest)


def translate_job(job_id: str):
    return translation_service.translate_job(sys.modules[__name__], job_id)


def translate_then_dub(job_id: str):
    try:
        check_pause(job_id)
        job = get_job(job_id)
        if job["status"] != "prepared" or not (DATA / f"{job_id}-translation-input.json").is_file():
            prepare(job_id)
        translate_job(job_id)
        check_pause(job_id)
        if get_job(job_id)["translation_status"] == "done":
            dub_job(job_id)
    except PauseRequested:
        mark_paused(job_id)
    except Exception as exc:
        update_job(job_id, translation_status="failed", translation_error=str(exc))


def process(job_id: str, reuse_download: bool = False, resume: bool = False):
    try:
        check_pause(job_id)
        job = get_job(job_id)
        log = DATA / f"{job_id}.log"
        if not resume:
            log.write_text("", encoding="utf-8")
        if job.get("source_url") and (not reuse_download or not Path(job["upload_path"]).is_file()):
            update_job(job_id, status="running", stage="download", error="")
            info = download_youtube(job["source_url"], Path(job["upload_path"]), log,
                                    YOUTUBE_CONFIG, YOUTUBE_COOKIES)
            if job["title"] == f"YouTube {info['id']}" and info.get("title"):
                update_job(job_id, title=info["title"][:160])
        check_pause(job_id)
        update_job(job_id, status="running", stage="import", error="")
        with heavy_media_stage(job_id, "视频准备与导入"):
            if job.get("batch_source_path"):
                prepare_batch_video(Path(job["batch_source_path"]), Path(job["upload_path"]), log)
            run = import_media(Path(job["upload_path"]), ROOT / "runs")
        check_pause(job_id)
        separate = bool(job["separate_vocals"])
        update_job(job_id, run_path=str(run))
        if separate:
            update_job(job_id, stage="separate")
            with heavy_media_stage(job_id, "人声分离"):
                with log.open("a", encoding="utf-8") as output:
                    result = subprocess.run([sys.executable, "-m", "vidlingua.pipeline.separate_audio", str(run)],
                                            cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, check=False)
            if result.returncode:
                raise RuntimeError(f"人声分离失败，退出码 {result.returncode}；请查看处理日志")
        check_pause(job_id)
        update_job(job_id, stage="transcribe")
        audio_source = "vocals" if separate else "original"
        asr_transcript = run / f"transcript_{audio_source}" / ("asr_vocals.json" if separate else "asr_audio.json")
        transcript = asr_transcript
        subtitle_imported = False
        if job.get("batch_source_path"):
            subtitle_source = matching_subtitle(Path(job["batch_source_path"]))
            if subtitle_source:
                try:
                    transcript = DATA / f"{job_id}-external-transcript.json"
                    reused = import_subtitle_transcript(subtitle_source, transcript)
                    subtitle_imported = True
                    with log.open("a", encoding="utf-8") as output:
                        output.write(f"批处理：使用同名字幕 {subtitle_source.name}，{'复用导入结果' if reused else '已导入'}；跳过 WhisperX\n")
                except (OSError, UnicodeError, ValueError) as exc:
                    transcript = asr_transcript
                    with log.open("a", encoding="utf-8") as output:
                        output.write(f"批处理：同名字幕不可用（{exc}），回退英文识别\n")
        with log.open("a", encoding="utf-8") as output:
            if not subtitle_imported:
                output.write(f"ASR 输入：{audio_source}，音频文件：{'asr_vocals.wav' if separate else 'asr_audio.wav'}，"
                             f"现有转写：{'复用并复查' if transcript.is_file() else '重新识别'}\n")
        if not subtitle_imported:
            with heavy_media_stage(job_id, "英文识别与漏句复查"):
                if not transcript.is_file():
                    with log.open("a", encoding="utf-8") as output:
                        result = subprocess.run([sys.executable, "-m", "vidlingua.pipeline.transcribe",
                                                 str(run), "--audio-source", audio_source],
                                                cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                                check=False)
                    if result.returncode or not transcript.is_file():
                        raise RuntimeError(f"WhisperX 失败，退出码 {result.returncode}；请查看处理日志")
                if transcript.is_file():
                    from vidlingua.pipeline.transcribe import recover_transcript_gaps
                    source_audio = run / ("asr_vocals.wav" if separate else "asr_audio.wav")
                    def record_asr(message: str) -> None:
                        with log.open("a", encoding="utf-8") as output:
                            output.write(message + "\n")
                    recover_transcript_gaps(source_audio, transcript, logger=record_asr)
        check_pause(job_id)
        update_job(job_id, stage="organize")
        duration = json.loads((run / "manifest.json").read_text(encoding="utf-8"))["media"]["duration_seconds"]
        if not segment_rows(job_id):
            save_segments(job_id, make_segments(transcript, duration))
        check_pause(job_id)
        update_job(job_id, stage="review", status="ready")
        translate_then_dub(job_id)
    except PauseRequested:
        mark_paused(job_id)
    except Exception as exc:
        update_job(job_id, status="failed", error=str(exc))


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/jobs")
def list_jobs():
    with connect() as conn:
        rows = conn.execute("""SELECT id,title,filename,status,stage,error,created_at,updated_at,
                               translation_status,dubbing_status,dubbing_engine,separate_vocals,source_url,batch_id,pause_requested
                               FROM jobs WHERE batch_id IS NULL ORDER BY created_at DESC""").fetchall()
        batch_ids = [row["id"] for row in conn.execute("SELECT id FROM batches").fetchall()]
    items = [dict(row) for row in rows]
    items.extend(batch_summary(batch_id, include_children=False) for batch_id in batch_ids)
    return sorted(items, key=lambda item: item["created_at"], reverse=True)


def batch_summary(batch_id: str, include_children: bool = True) -> dict:
    with connect() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise HTTPException(404, "批处理任务不存在")
        children = [dict(row) for row in conn.execute(
            """SELECT id,title,filename,status,stage,error,created_at,updated_at,
                      translation_status,translation_error,dubbing_status,dubbing_error,
                      dubbing_engine,separate_vocals,batch_source_path,pause_requested
               FROM jobs WHERE batch_id=? ORDER BY filename COLLATE NOCASE,id""", (batch_id,)
        ).fetchall()]
    completed = sum(child["dubbing_status"] == "done" for child in children)
    failed = sum(child["status"] in ("failed", "interrupted") or
                 child["translation_status"] in ("failed", "interrupted") or
                 child["dubbing_status"] in ("failed", "interrupted") for child in children)
    review = sum(child["dubbing_status"] == "needs_review" for child in children)
    status = ("done" if children and completed == len(children) else
              "running" if any(child["status"] in ("queued", "running") or
                               child["translation_status"] in ("queued", "correcting", "glossary", "running") or
                               child["dubbing_status"] in ("queued", "running") for child in children) else
              "paused" if any(child["status"] == "paused" for child in children) else
              "failed" if failed else "review" if review else "created")
    output_dir = str(Path(batch["source_dir"]) / "Translate")
    summary = {**dict(batch), "kind": "batch", "filename": batch["source_dir"],
               "supports_actions": True,
               "status": status, "translation_status": "", "dubbing_status": "",
               "source_url": "", "dubbing_engine": "", "updated_at": batch["created_at"],
               "count": len(children), "completed": completed, "failed": failed,
               "review": review, "output_dir": output_dir}
    if include_children:
        summary["children"] = children
    return summary


class BatchCreate(BaseModel):
    path: str
    separate_vocals: bool = True
    dubbing_engine: str = "doubao"


def pick_folder_dialog() -> str | None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "vidlingua.api.folder_picker"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "选择文件夹超时，请重试或手动输入路径") from exc
    if result.returncode:
        raise HTTPException(503, f"无法打开文件夹选择窗口：{result.stderr.strip()[-500:]}")
    try:
        selected = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise HTTPException(503, "文件夹选择窗口未返回有效路径") from exc
    if selected and not Path(selected).is_dir():
        raise HTTPException(400, "所选文件夹已不存在")
    return selected


@app.post("/api/batches/select-folder")
def select_batch_folder():
    if not folder_picker_lock.acquire(blocking=False):
        raise HTTPException(409, "已有文件夹选择窗口，请先完成当前选择")
    try:
        return {"path": pick_folder_dialog()}
    finally:
        folder_picker_lock.release()


@app.post("/api/batches")
def create_batch(payload: BatchCreate):
    if not payload.path.strip():
        raise HTTPException(400, "请输入文件夹路径")
    try:
        videos = scan_directory(Path(payload.path.strip()))
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    engine = _valid_engine(payload.dubbing_engine)
    directory = videos[0].parent
    batch_id = uuid.uuid4().hex[:12]
    timestamp = now()
    jobs = []
    with db_lock, connect() as conn:
        conn.execute("INSERT INTO batches (id,title,source_dir,created_at) VALUES (?,?,?,?)",
                     (batch_id, directory.name, str(directory), timestamp))
        for source in videos:
            job_id = uuid.uuid4().hex[:12]
            upload = UPLOADS / job_id / f"{source.stem}.mp4"
            conn.execute("""INSERT INTO jobs
                (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,
                 created_at,updated_at,separate_vocals,dubbing_engine,batch_id,batch_source_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, source.stem, "", "", source.name, str(upload), None, "queued", "import", "",
                 timestamp, timestamp, int(payload.separate_vocals), engine, batch_id, str(source)))
            jobs.append(job_id)
    for job_id in jobs:
        executor.submit(process, job_id)
    return batch_summary(batch_id)


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    return batch_summary(batch_id)


@app.post("/api/batches/{batch_id}/pause")
def pause_batch(batch_id: str):
    batch = batch_summary(batch_id)
    for child in batch["children"]:
        if (child["status"] in ("queued", "running") or
                child["translation_status"] in ("queued", "correcting", "glossary", "running") or
                child["dubbing_status"] in ("queued", "running")) and not child["pause_requested"]:
            pause_job(child["id"])
    return batch_summary(batch_id)


@app.post("/api/batches/{batch_id}/resume")
def resume_batch(batch_id: str):
    batch = batch_summary(batch_id)
    for child in batch["children"]:
        if child["status"] == "paused":
            resume_job(child["id"])
    return batch_summary(batch_id)


@app.post("/api/batches/{batch_id}/rerun")
def rerun_batch(batch_id: str):
    batch = batch_summary(batch_id)
    _rerun_jobs([child["id"] for child in batch["children"]])
    return batch_summary(batch_id)


@app.delete("/api/batches/{batch_id}")
def delete_batch(batch_id: str):
    with db_lock:
        batch = batch_summary(batch_id)
        ids = [child["id"] for child in batch["children"]]
        if ids:
            _delete_jobs(ids)
        with connect() as conn:
            conn.execute("DELETE FROM batches WHERE id=?", (batch_id,))
    return {"deleted": batch_id}


class JobSelection(BaseModel):
    ids: list[str]


def _selected_jobs(ids: list[str]) -> list[dict]:
    if not ids or len(set(ids)) != len(ids):
        raise HTTPException(400, "请选择不同的任务")
    jobs = [get_job(job_id) for job_id in ids]
    for job in jobs:
        if job["id"] in _manual_edit_running:
            raise HTTPException(409, f"任务 {job['title']} 正在导出剪辑版，请完成后再操作")
        if (job["status"] in ("queued", "running")
                or job["translation_status"] in ("queued", "correcting", "glossary", "running")
                or job["dubbing_status"] in ("queued", "running")):
            raise HTTPException(409, f"任务 {job['title']} 正在运行，请完成后再操作")
    return jobs


def _job_files(job_id: str) -> list[Path]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", job_id):
        raise HTTPException(400, "任务 ID 无效")
    return [path for path in DATA.iterdir()
            if path.is_file() and (path.name.startswith(f"{job_id}-") or path.name == f"{job_id}.log")]


def _remove_job_directory(parent: Path, job_id: str) -> None:
    root = parent.resolve()
    target = (root / job_id).resolve()
    if not root.is_relative_to(DATA.resolve()) or target.parent != root:
        raise RuntimeError("任务目录超出预期范围，停止删除")
    if target.is_dir():
        shutil.rmtree(target)


def _rerun_jobs(ids: list[str]) -> list[dict]:
    with db_lock:
        jobs = _selected_jobs(ids)
        for job in jobs:
            if (not job.get("source_url") and not Path(job["upload_path"]).is_file()
                    and not (job.get("batch_source_path") and Path(job["batch_source_path"]).is_file())):
                raise HTTPException(409, f"任务 {job['title']} 的原视频不存在，无法重跑")
        for job in jobs:
            job_id = job["id"]
            for path in _job_files(job_id):
                path.unlink()
            with connect() as conn:
                conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
                conn.execute("""UPDATE jobs SET status='queued',stage='import',error='',
                    translation_status='not_started',translation_progress=0,translation_total=0,translation_error='',
                    dubbing_status='not_started',dubbing_progress=0,dubbing_total=0,dubbing_error='',pause_requested=0,updated_at=?
                    WHERE id=?""", (now(), job_id))
        for job in jobs:
            executor.submit(process, job["id"], True)
    return [get_job(job_id) for job_id in ids]


def _delete_jobs(ids: list[str]) -> list[str]:
    with db_lock:
        jobs = _selected_jobs(ids)
        for job in jobs:
            job_id = job["id"]
            for path in _job_files(job_id):
                path.unlink()
            for parent in (UPLOADS, DATA / "dubbing", DATA / "backups"):
                _remove_job_directory(parent, job_id)
            with connect() as conn:
                conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
                conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return ids


def _expand_bulk_selection(ids: list[str]) -> tuple[list[str], list[str]]:
    if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
        raise HTTPException(400, "请选择 1–100 个不同的任务")
    with connect() as conn:
        batches = {row["id"] for row in conn.execute(
            f"SELECT id FROM batches WHERE id IN ({','.join('?' for _ in ids)})", ids)}
    expanded = []
    for item_id in ids:
        if item_id in batches:
            expanded.extend(child["id"] for child in batch_summary(item_id)["children"])
        else:
            get_job(item_id)
            expanded.append(item_id)
    return list(dict.fromkeys(expanded)), [item_id for item_id in ids if item_id in batches]


@app.post("/api/jobs/bulk/rerun")
def bulk_rerun_jobs(selection: JobSelection):
    ids, _ = _expand_bulk_selection(selection.ids)
    return {"jobs": _rerun_jobs(ids) if ids else []}


@app.delete("/api/jobs/bulk")
def bulk_delete_jobs(selection: JobSelection):
    with db_lock:
        ids, batches = _expand_bulk_selection(selection.ids)
        if ids:
            _delete_jobs(ids)
        with connect() as conn:
            conn.executemany("DELETE FROM batches WHERE id=?", [(batch_id,) for batch_id in batches])
    return {"deleted": selection.ids}


@app.post("/api/jobs")
def create_job(file: UploadFile = File(...), title: str = Form(""),
               separate_vocals: bool = Form(True), dubbing_engine: str = Form("doubao"),
               publish_to_bilibili: bool = Form(False)):
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
    engine = _valid_engine(dubbing_engine if isinstance(dubbing_engine, str) else "doubao")
    timestamp = now()
    with db_lock, connect() as conn:
        conn.execute("""INSERT INTO jobs
            (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at,separate_vocals,dubbing_engine,publish_to_bilibili)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (job_id, title.strip() or Path(filename).stem, "", "",
                      filename, str(upload), None, "created", "upload", "", timestamp, timestamp,
                      int(separate_vocals), engine,
                      int(publish_to_bilibili) if isinstance(publish_to_bilibili, bool) else 0))
    return get_job(job_id)


class YouTubeJobCreate(BaseModel):
    url: str
    title: str = ""
    separate_vocals: bool = True
    dubbing_engine: str = "doubao"
    publish_to_bilibili: bool = True


class ApiJobCreate(BaseModel):
    source: Literal["local", "youtube", "batch"]
    path: str = ""
    url: str = ""
    title: str = ""
    separate_vocals: bool = True
    dubbing_engine: str = "doubao"
    publish_to_bilibili: bool | None = None


@app.post("/api/jobs/create", status_code=202)
def create_job_by_api(payload: ApiJobCreate):
    """Create and queue a job from a server-side path, YouTube URL, or folder."""
    if payload.source == "batch":
        batch = create_batch(BatchCreate(path=payload.path, separate_vocals=payload.separate_vocals,
                                         dubbing_engine=payload.dubbing_engine))
        return {**batch, "detail_url": f"/#batch/{batch['id']}"}
    if payload.source == "youtube":
        if not payload.url.strip():
            raise HTTPException(400, "请输入 YouTube 地址")
        job = create_youtube_job(YouTubeJobCreate(
            url=payload.url, title=payload.title, separate_vocals=payload.separate_vocals,
            dubbing_engine=payload.dubbing_engine,
            publish_to_bilibili=(payload.publish_to_bilibili
                                 if payload.publish_to_bilibili is not None else True)))
        if not job["already_exists"]:
            job = {**run_job(job["id"]), "already_exists": False}
        return {**job, "kind": "job", "detail_url": f"/#job/{job['id']}"}

    if not payload.path.strip():
        raise HTTPException(400, "请输入视频文件路径")
    try:
        source = Path(payload.path.strip()).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("路径不是视频文件")
        if source.suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError("不支持的视频格式")
        if source.stat().st_size == 0:
            raise ValueError("视频文件为空")
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    engine = _valid_engine(payload.dubbing_engine)
    job_id = uuid.uuid4().hex[:12]
    upload = UPLOADS / job_id / f"{source.stem}.mp4"
    timestamp = now()
    with db_lock, connect() as conn:
        conn.execute("""INSERT INTO jobs
            (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,
             created_at,updated_at,separate_vocals,dubbing_engine,batch_source_path,publish_to_bilibili)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, payload.title.strip() or source.stem, "", "", source.name, str(upload),
             None, "queued", "import", "", timestamp, timestamp, int(payload.separate_vocals),
             engine, str(source), int(bool(payload.publish_to_bilibili))))
    executor.submit(process, job_id)
    return {**get_job(job_id), "kind": "job", "detail_url": f"/#job/{job_id}"}


class YouTubeConfigEdit(BaseModel):
    proxy_mode: str = "system"
    proxy_port: str = ""
    cookie_content: str | None = None
    remove_cookies: bool = False


@app.get("/api/youtube/config")
def youtube_config():
    return {**load_youtube_config(YOUTUBE_CONFIG),
            "cookie_configured": YOUTUBE_COOKIES.is_file() and YOUTUBE_COOKIES.stat().st_size > 0}


@app.put("/api/youtube/config")
def configure_youtube(edit: YouTubeConfigEdit):
    try:
        config = validate_youtube_config(edit.model_dump())
        if edit.cookie_content is not None and edit.remove_cookies:
            raise ValueError("不能同时上传和移除 YouTube cookies")
        if edit.cookie_content is not None:
            save_youtube_cookies(YOUTUBE_COOKIES, edit.cookie_content)
        save_youtube_config(YOUTUBE_CONFIG, config)
        if edit.remove_cookies:
            YOUTUBE_COOKIES.unlink(missing_ok=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return youtube_config()


@app.post("/api/youtube/test")
def test_youtube():
    try:
        return test_youtube_connection(YOUTUBE_CONFIG, YOUTUBE_COOKIES)
    except Exception as exc:
        raise HTTPException(502, f"YouTube 连接测试失败：{exc}") from exc


@app.post("/api/jobs/youtube")
def create_youtube_job(payload: YouTubeJobCreate):
    try:
        canonical, video_id = canonical_youtube_url(payload.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with db_lock, connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM jobs WHERE source_url=? ORDER BY created_at DESC LIMIT 1", (canonical,)
        ).fetchone()
        if existing:
            job_id = existing["id"]
        else:
            job_id = uuid.uuid4().hex[:12]
            filename = f"youtube-{video_id}.mp4"
            upload = UPLOADS / job_id / filename
            timestamp = now()
            conn.execute("""INSERT INTO jobs
                (id,title,subject,glossary,filename,upload_path,run_path,status,stage,error,created_at,updated_at,separate_vocals,source_url,dubbing_engine,publish_to_bilibili)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (job_id, payload.title.strip() or f"YouTube {video_id}", "", "",
                          filename, str(upload), None, "created", "download", "", timestamp, timestamp,
                          int(payload.separate_vocals), canonical, _valid_engine(payload.dubbing_engine),
                          int(payload.publish_to_bilibili)))
    job = get_job(job_id)
    job["already_exists"] = existing is not None
    return job


@app.post("/api/jobs/{job_id}/run")
def run_job(job_id: str):
    job = get_job(job_id)
    if job["status"] in ("queued", "running", "paused") or job["pause_requested"]:
        raise HTTPException(409, "任务正在处理或已暂停，请使用继续任务")
    update_job(job_id, status="queued", error="")
    executor.submit(process, job_id)
    return get_job(job_id)


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str):
    with db_lock:
        job = get_job(job_id)
        if job["pause_requested"]:
            return job
        active = (job["status"] in ("queued", "running") or
                  job["translation_status"] in ("queued", "correcting", "glossary", "running") or
                  job["dubbing_status"] in ("queued", "running"))
        if not active:
            raise HTTPException(409, "任务当前没有正在运行的处理阶段")
        update_job(job_id, pause_requested=1)
    return get_job(job_id)


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    with db_lock:
        job = get_job(job_id)
        if job["status"] != "paused":
            raise HTTPException(409, "只有已暂停的任务可以继续")
        if job["dubbing_status"] == "paused" and job["translation_status"] == "done":
            target = dub_job
            update_job(job_id, pause_requested=0, status="ready", dubbing_status="queued")
        elif job["translation_status"] in ("paused", "done") and (DATA / f"{job_id}-translation-input.json").is_file():
            target = translate_then_dub
            update_job(job_id, pause_requested=0, status="prepared", translation_status="queued")
        else:
            target = process
            update_job(job_id, pause_requested=0, status="queued")
        if target is process:
            executor.submit(target, job_id, True, True)
        else:
            executor.submit(target, job_id)
    return get_job(job_id)


@app.post("/api/jobs/{job_id}/rerun")
def rerun_job(job_id: str):
    return _rerun_jobs([job_id])[0]


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    _delete_jobs([job_id])
    return {"deleted": job_id}


def _asr_diagnostics_path(job: dict) -> Path:
    source = "vocals" if job["separate_vocals"] else "original"
    stem = "asr_vocals" if job["separate_vocals"] else "asr_audio"
    return Path(job["run_path"]) / f"transcript_{source}" / f"{stem}.diagnostics.json"


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    job = get_job(job_id)
    if job.get("batch_id"):
        with connect() as conn:
            batch = conn.execute("SELECT source_dir FROM batches WHERE id=?", (job["batch_id"],)).fetchone()
        job["batch_output_path"] = (str(Path(batch["source_dir"]) / "Translate" /
                                        f"{Path(job['batch_source_path']).name}.translated.mp4")
                                    if batch else None)
    cover = Path(job["upload_path"]).with_name("cover.jpg") if job.get("source_url") else None
    job["cover_path"] = str(cover.resolve()) if cover and cover.is_file() else None
    segments = segment_rows(job_id)
    job["segment_count"] = len(segments)
    job["review_count"] = sum(bool(s["flags"]) and not s["reviewed"] for s in segments)
    job["term_candidates"] = term_candidates(segments)
    job["english_correction_ready"] = (DATA / f"{job_id}-corrected-en.srt").is_file()
    job["dubbing_report_available"] = (DATA / f"{job_id}-dubbing-report.json").is_file()
    job["dubbed_video_available"] = (job["dubbing_status"] == "done"
                                     and (DATA / f"{job_id}-dubbed.mp4").is_file())
    job["asr_diagnostics_available"] = (_asr_diagnostics_path(job).is_file()
                                        if job.get("run_path") else False)
    job["stage_progress"] = stage_progress(job, job_id)
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
    if job["translation_status"] in ("queued", "correcting", "glossary", "running"):
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
    if job["translation_status"] in ("queued", "correcting", "glossary", "running"):
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
    if job["translation_status"] in ("queued", "correcting", "glossary", "running"):
        raise HTTPException(409, "翻译进行中")
    if job["status"] not in ("ready", "prepared") or not job["run_path"]:
        raise HTTPException(409, "请先完成转写")
    segments = segment_rows(job_id)
    if not segments:
        raise HTTPException(409, "没有可导出的句段")
    try:
        audio_name = "asr_vocals.wav" if job["separate_vocals"] else "asr_audio.wav"
        realign_edits(segments, Path(job["run_path"]) / audio_name)
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
    job = get_job(job_id)
    path = DATA / f"{job_id}-translation-input.json"
    if not path.is_file():
        raise HTTPException(404, "请先准备译前包")
    return FileResponse(path, media_type="application/json",
                        filename=download_filename(job, ".translation.json"))


@app.get("/api/jobs/{job_id}/corrected-en.srt")
def download_corrected_english(job_id: str):
    job = get_job(job_id)
    path = DATA / f"{job_id}-corrected-en.srt"
    if not path.is_file():
        raise HTTPException(404, "英文校正尚未完成")
    return FileResponse(path, media_type="text/plain; charset=utf-8",
                        filename=download_filename(job, ".en.srt"))


@app.get("/api/jobs/{job_id}/media/{kind}")
def media(job_id: str, kind: str):
    job = get_job(job_id)
    if not job["run_path"]:
        raise HTTPException(404, "媒体尚未导入")
    run = Path(job["run_path"])
    names = {"video": "source.mp4",
             "vocals": "asr_vocals.wav" if job["separate_vocals"] else "asr_audio.wav"}
    if job["separate_vocals"]:
        names["instrumental"] = "separated/instrumental.flac"
    if kind not in names:
        raise HTTPException(404)
    path = run / names[kind]
    if not path.is_file():
        raise HTTPException(404, "媒体尚未就绪")
    return FileResponse(path)


@app.get("/api/jobs/{job_id}/log")
def log(job_id: str):
    job = get_job(job_id)
    path = DATA / f"{job_id}.log"
    lines = []
    if path.is_file():
        with path.open("rb") as source:
            source.seek(max(0, path.stat().st_size - 65536))
            tail = source.read().decode("utf-8", errors="replace")
        tail = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", tail).replace("\r", "\n")
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
    summary = (f"[当前状态] 处理 {job['status']} · 翻译 {job['translation_status']} · "
               f"配音 {job['dubbing_status']} · {stage_progress(job, job_id)['label']}")
    message = job.get("dubbing_error") or job.get("translation_error") or job.get("error")
    lines = lines[-80:] + [summary]
    if message:
        lines.append(f"[提醒] {message}")
    return {"text": "\n".join(lines)[-12000:]}


@app.get("/api/jobs/{job_id}/asr-diagnostics.json")
def download_asr_diagnostics(job_id: str):
    job = get_job(job_id)
    if not job.get("run_path"):
        raise HTTPException(404, "识别诊断尚未生成")
    path = _asr_diagnostics_path(job)
    if not path.is_file():
        raise HTTPException(404, "识别诊断尚未生成")
    return FileResponse(path, media_type="application/json",
                        filename=download_filename(job, ".asr.json"))


class DeepSeekConfigEdit(BaseModel):
    model: str = DEFAULTS["model"]
    temperature: float = DEFAULTS["temperature"]
    max_tokens: int = DEFAULTS["max_tokens"]
    concurrency: int = DEFAULTS["concurrency"]
    api_key: str | None = None
    remove_key: bool = False


class WatermarkConfigEdit(BaseModel):
    enabled: bool = WATERMARK_DEFAULTS["enabled"]
    text: str = WATERMARK_DEFAULTS["text"]
    position: str = WATERMARK_DEFAULTS["position"]
    opacity: float = WATERMARK_DEFAULTS["opacity"]


@app.get("/api/watermark/config")
def watermark_config():
    return settings_service.watermark_config(sys.modules[__name__])


@app.put("/api/watermark/config")
def configure_watermark(edit: WatermarkConfigEdit):
    return settings_service.configure_watermark(sys.modules[__name__], edit)


class SubtitleConfigEdit(BaseModel):
    enabled: bool = SUBTITLE_DEFAULTS["enabled"]
    mode: str = SUBTITLE_DEFAULTS["mode"]
    font_size: int = SUBTITLE_DEFAULTS["font_size"]
    margin_bottom: int = SUBTITLE_DEFAULTS["margin_bottom"]
    outline: int = SUBTITLE_DEFAULTS["outline"]


@app.get("/api/subtitles/config")
def subtitle_config():
    return settings_service.subtitle_config(sys.modules[__name__])


@app.put("/api/subtitles/config")
def configure_subtitles(edit: SubtitleConfigEdit):
    return settings_service.configure_subtitles(sys.modules[__name__], edit)


@app.get("/api/deepseek/config")
def deepseek_config():
    return settings_service.deepseek_config(sys.modules[__name__])


@app.put("/api/deepseek/config")
def configure_deepseek(edit: DeepSeekConfigEdit):
    return settings_service.configure_deepseek(sys.modules[__name__], edit)


@app.post("/api/deepseek/test")
def test_deepseek():
    return settings_service.test_deepseek(sys.modules[__name__])


@app.post("/api/jobs/{job_id}/translate")
def start_translation(job_id: str, force: bool = False):
    job = get_job(job_id)
    if job["status"] == "paused" or job["pause_requested"]:
        raise HTTPException(409, "任务已暂停，请先继续任务")
    if job["status"] not in ("ready", "prepared"):
        raise HTTPException(409, "请先完成英文转写")
    key = get_api_key()
    if not key:
        raise HTTPException(409, "请先配置 DeepSeek API Key")
    try:
        validate_api_key(key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if job["translation_status"] in ("queued", "correcting", "glossary", "running"):
        raise HTTPException(409, "翻译正在进行")
    if job["translation_status"] == "done":
        if not force:
            raise HTTPException(409, "翻译已完成；如需重新分析课程并翻译，请明确选择重新翻译")
        invalidate_dubbing(job_id)
        for suffix in ("corrected-english.json", "corrected-en.srt", "glossary.json",
                       "translation.json", "zh.srt", "bilingual.srt"):
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
    job = get_job(job_id)
    path = DATA / f"{job_id}-zh.srt"
    if not path.is_file():
        raise HTTPException(404, "尚无中文字幕")
    return FileResponse(path, media_type="text/plain; charset=utf-8",
                        filename=download_filename(job, ".zh.srt"))


@app.get("/api/jobs/{job_id}/bilingual.srt")
def download_bilingual_srt(job_id: str):
    job = get_job(job_id)
    if job["translation_status"] != "done":
        raise HTTPException(404, "尚无双语字幕")
    path = DATA / f"{job_id}-bilingual.srt"
    result = translation_result(job_id)
    items = [item for group in result["groups"] for item in group["segments"]]
    bilingual = make_bilingual_srt(items)
    if not path.is_file() or path.read_text(encoding="utf-8-sig") != bilingual:
        path.write_text(bilingual, encoding="utf-8-sig")
    return FileResponse(path, media_type="text/plain; charset=utf-8",
                        filename=download_filename(job, ".bilingual.srt"))


class AzureConfigEdit(BaseModel):
    region: str = AZURE_DEFAULTS["region"]
    voice: str = AZURE_DEFAULTS["voice"]
    hd_temperature: float = AZURE_DEFAULTS["hd_temperature"]
    per_key_concurrency: int = AZURE_DEFAULTS["per_key_concurrency"]
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
    return settings_service.azure_config(sys.modules[__name__])


@app.put("/api/azure/config")
def configure_azure(edit: AzureConfigEdit):
    return settings_service.configure_azure(sys.modules[__name__], edit)


@app.get("/api/azure/voices")
@app.post("/api/azure/test")
def test_azure():
    return settings_service.test_azure(sys.modules[__name__])


class AzureKeysCheck(BaseModel):
    region: str | None = None


@app.post("/api/azure/keys/check")
def check_azure_keys(request: AzureKeysCheck):
    return settings_service.check_azure_keys(sys.modules[__name__], request)


class DoubaoConfigEdit(BaseModel):
    resource_id: str = DOUBAO_DEFAULTS["resource_id"]
    speaker: str = DOUBAO_DEFAULTS["speaker"]
    concurrency: int = DOUBAO_DEFAULTS["concurrency"]
    calmness: int = DOUBAO_DEFAULTS["calmness"]
    api_key: str | None = None
    remove_key: bool = False


@app.get("/api/doubao/config")
def doubao_config():
    return settings_service.doubao_config(sys.modules[__name__])


@app.put("/api/doubao/config")
def configure_doubao(edit: DoubaoConfigEdit):
    return settings_service.configure_doubao(sys.modules[__name__], edit)


@app.post("/api/doubao/test")
def test_doubao():
    return settings_service.test_doubao(sys.modules[__name__])


def _valid_engine(engine: str) -> str:
    if engine not in ("azure", "doubao"):
        raise HTTPException(400, "不支持的配音引擎")
    return engine


def dub_job(job_id: str):
    result = dubbing_service.dub_job(sys.modules[__name__], job_id)
    job = get_job(job_id)
    if (not job.get("batch_id") and job.get("publish_to_bilibili")
            and job["dubbing_status"] == "done"):
        try:
            _auto_publish_bilibili(job_id)
        except Exception as exc:
            meta = _bilibili_view(job_id)
            meta.update(status="error", message="自动投稿失败", error=str(exc) or "自动投稿失败")
            bilibili_provider.save_job_meta(DATA / f"{job_id}-bilibili.json", meta)
    if job.get("batch_id") and job["dubbing_status"] == "done":
        try:
            check_pause(job_id)
            update_job(job_id, stage="publish", dubbing_status="running")
            with connect() as conn:
                batch = conn.execute("SELECT source_dir FROM batches WHERE id=?", (job["batch_id"],)).fetchone()
            if batch is None:
                raise RuntimeError("批处理父任务不存在")
            target = (Path(batch["source_dir"]) / "Translate" /
                      f"{Path(job['batch_source_path']).name}.translated.mp4")
            with heavy_media_stage(job_id, "成片写入目标文件夹"):
                publish_batch_video(DATA / f"{job_id}-dubbed.mp4", target)
            check_pause(job_id)
            with (DATA / f"{job_id}.log").open("a", encoding="utf-8") as log:
                log.write(f"批处理：成片已输出到 {target}\n")
            update_job(job_id, stage="done", dubbing_status="done")
        except PauseRequested:
            mark_paused(job_id)
        except Exception as exc:
            update_job(job_id, dubbing_status="failed", dubbing_error=f"成片输出失败：{exc}")
    return result


@app.post("/api/jobs/{job_id}/dub")
def start_dubbing(job_id: str):
    job = get_job(job_id)
    if job["status"] == "paused" or job["pause_requested"]:
        raise HTTPException(409, "任务已暂停，请先继续任务")
    if job_id in _manual_edit_running:
        raise HTTPException(409, "剪辑导出正在进行，请完成后再重新配音")
    if job["translation_status"] != "done":
        raise HTTPException(409, "请先完成中文翻译")
    if job["dubbing_status"] in ("queued", "running"):
        raise HTTPException(409, "配音正在进行")
    engine = _valid_engine(job.get("dubbing_engine") or "azure")
    if engine == "doubao":
        key = get_doubao_key()
        if not key:
            raise HTTPException(409, "请先配置豆包语音 API Key")
        try:
            validate_doubao_key(key)
            validate_doubao_config(load_doubao_config(DOUBAO_CONFIG))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    else:
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
    job = get_job(job_id)
    path = DATA / f"{job_id}-voice.wav"
    if not path.is_file():
        raise HTTPException(404, "尚无中文配音轨")
    return FileResponse(path, media_type="audio/wav", filename=download_filename(job, ".wav"))


@app.get("/api/jobs/{job_id}/dubbed.mp4")
def download_dubbed_video(job_id: str):
    job = get_job(job_id)
    path = DATA / f"{job_id}-dubbed.mp4"
    if job["dubbing_status"] != "done" or not path.is_file():
        raise HTTPException(404, "配音视频尚未就绪")
    return FileResponse(path, media_type="video/mp4", filename=download_filename(job, ".mp4"))


@app.get("/api/jobs/{job_id}/preview.mp4")
def preview_dubbed_video(job_id: str):
    job = get_job(job_id)
    path = DATA / f"{job_id}-dubbed.mp4"
    if job["dubbing_status"] != "done" or not path.is_file():
        raise HTTPException(404, "配音视频尚未就绪")
    return FileResponse(path, media_type="video/mp4")


def _manual_edit_paths(job_id: str) -> tuple[Path, Path, Path]:
    return (DATA / f"{job_id}-manual-edit.json", DATA / f"{job_id}-dubbed.mp4",
            DATA / f"{job_id}-edited.mp4")


def _manual_edit_source(job_id: str) -> Path:
    job = get_job(job_id)
    source = DATA / f"{job_id}-dubbed.mp4"
    if job["dubbing_status"] != "done" or not source.is_file():
        raise HTTPException(409, "请先完成配音成片")
    return source


def _read_manual_edit(job_id: str) -> dict:
    state_path, source, output = _manual_edit_paths(job_id)
    duration = manual_edit.duration_seconds(source)
    identity = manual_edit.source_id(source)
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("source_id") != identity:
            state = {}
    else:
        state = {}
    if not state:
        state = {"source_id": identity, "ranges": [{"start": 0, "end": round(duration, 3)}],
                 "status": "draft", "error": "", "revision": uuid.uuid4().hex}
    if state["status"] in ("queued", "rendering") and not job_id in _manual_edit_running:
        state["status"] = "interrupted"
        state["error"] = "剪辑导出已中断，请重新导出"
    return {**state, "duration": duration,
            "edited_video_available": state["status"] == "done" and output.is_file()}


def _write_manual_edit(job_id: str, state: dict) -> None:
    path = DATA / f"{job_id}-manual-edit.json"
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({k: v for k, v in state.items() if k not in
                                         ("duration", "edited_video_available")}, ensure_ascii=False),
                             encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


_manual_edit_running: set[str] = set()


class ManualEditDraft(BaseModel):
    ranges: list[dict]


@app.get("/api/jobs/{job_id}/manual-edit")
def get_manual_edit(job_id: str):
    _manual_edit_source(job_id)
    with db_lock:
        return _read_manual_edit(job_id)


@app.put("/api/jobs/{job_id}/manual-edit")
def save_manual_edit(job_id: str, body: ManualEditDraft):
    _manual_edit_source(job_id)
    with db_lock:
        state = _read_manual_edit(job_id)
        if job_id in _manual_edit_running:
            raise HTTPException(409, "剪辑导出正在进行，请完成后修改")
        try:
            ranges = manual_edit.validate_ranges(body.ranges, state["duration"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if ranges != state["ranges"]:
            state.update(ranges=ranges, status="draft", error="", revision=uuid.uuid4().hex)
        _write_manual_edit(job_id, state)
        return _read_manual_edit(job_id)


def _export_manual_edit(job_id: str, revision: str) -> None:
    try:
        with db_lock:
            source = _manual_edit_source(job_id)
            state = _read_manual_edit(job_id)
            if state["revision"] != revision:
                return
            state["status"] = "rendering"
            _write_manual_edit(job_id, state)
        _, _, output = _manual_edit_paths(job_id)
        with heavy_media_stage(job_id, "手动剪辑视频编码", pause_sensitive=False):
            manual_edit.render_edit(source, output, state["ranges"], DATA / f"{job_id}.log")
        for kind in ("zh", "bilingual"):
            original = DATA / f"{job_id}-dubbed-{kind}.srt"
            edited = DATA / f"{job_id}-edited-{kind}.srt"
            if original.is_file():
                edited.write_text(manual_edit.retime_srt(original.read_text(encoding="utf-8-sig"),
                                                         state["ranges"]), encoding="utf-8-sig")
        with db_lock:
            state["status"] = "done"
            state["error"] = ""
            _write_manual_edit(job_id, state)
    except Exception as exc:
        with db_lock:
            state_path = DATA / f"{job_id}-manual-edit.json"
            if state_path.is_file():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if state.get("revision") == revision:
                    state.update(status="failed", error=str(exc))
                    _write_manual_edit(job_id, state)
        with (DATA / f"{job_id}.log").open("a", encoding="utf-8") as log:
            log.write(f"手动剪辑导出失败：{exc}\n")
    finally:
        with db_lock:
            _manual_edit_running.discard(job_id)


@app.post("/api/jobs/{job_id}/manual-edit/export")
def export_manual_edit(job_id: str):
    _manual_edit_source(job_id)
    with db_lock:
        if job_id in _manual_edit_running:
            raise HTTPException(409, "剪辑导出正在进行")
        state = _read_manual_edit(job_id)
        state["status"] = "queued"
        state["error"] = ""
        state["export_id"] = uuid.uuid4().hex
        _write_manual_edit(job_id, state)
        _manual_edit_running.add(job_id)
        executor.submit(_export_manual_edit, job_id, state["revision"])
        return _read_manual_edit(job_id)


@app.get("/api/jobs/{job_id}/edited-preview.mp4")
def preview_edited_video(job_id: str):
    _manual_edit_source(job_id)
    state = _read_manual_edit(job_id)
    if not state["edited_video_available"]:
        raise HTTPException(404, "剪辑成片尚未就绪")
    return FileResponse(DATA / f"{job_id}-edited.mp4", media_type="video/mp4")


@app.get("/api/jobs/{job_id}/edited.mp4")
def download_edited_video(job_id: str):
    job = get_job(job_id)
    preview_edited_video(job_id)
    return FileResponse(DATA / f"{job_id}-edited.mp4", media_type="video/mp4",
                        filename=download_filename(job, "-剪辑版.mp4"))


@app.get("/api/jobs/{job_id}/edited-{kind}.srt")
def download_edited_srt(job_id: str, kind: str):
    if kind not in ("zh", "bilingual"):
        raise HTTPException(404, "字幕类型不存在")
    preview_edited_video(job_id)
    path = DATA / f"{job_id}-edited-{kind}.srt"
    if not path.is_file():
        raise HTTPException(404, "剪辑字幕不存在")
    return FileResponse(path, media_type="text/plain; charset=utf-8",
                        filename=download_filename(get_job(job_id), f"-剪辑版-{kind}.srt"))


@app.get("/api/jobs/{job_id}/edit-thumb/{index}.jpg")
def manual_edit_thumbnail(job_id: str, index: int):
    source = _manual_edit_source(job_id)
    duration = manual_edit.duration_seconds(source)
    count = min(40, max(1, math.ceil(duration / 45)))
    if index < 0 or index >= count:
        raise HTTPException(404, "缩略图序号无效")
    path = DATA / f"{job_id}-edit-thumb-{index:02}.jpg"
    if not path.is_file() or path.stat().st_mtime_ns < source.stat().st_mtime_ns:
        temporary = DATA / f".{job_id}-edit-thumb-{index:02}-{uuid.uuid4().hex}.jpg"
        try:
            at = min(duration - .05, (index + .5) * duration / count)
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-ss", str(max(0, at)), "-i", str(source), "-frames:v", "1",
                       "-vf", "scale=240:-2", "-q:v", "5", str(temporary)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
            if result.returncode or not temporary.is_file():
                raise RuntimeError(result.stderr[-300:] or "缩略图生成失败")
            temporary.replace(path)
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            raise HTTPException(503, f"无法生成缩略图：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/jobs/{job_id}/dubbed-zh.srt")
def download_dubbed_srt(job_id: str):
    job = get_job(job_id)
    path = DATA / f"{job_id}-dubbed-zh.srt"
    if job["dubbing_status"] != "done" or not path.is_file():
        raise HTTPException(404, "配音字幕尚未就绪")
    return FileResponse(path, media_type="text/plain; charset=utf-8",
                        filename=download_filename(job, ".srt"))


@app.get("/api/jobs/{job_id}/dubbed-bilingual.srt")
def download_dubbed_bilingual_srt(job_id: str):
    job = get_job(job_id)
    path = DATA / f"{job_id}-dubbed-bilingual.srt"
    if job["dubbing_status"] != "done" or not path.is_file():
        raise HTTPException(404, "配音双语字幕尚未就绪")
    return FileResponse(path, media_type="text/plain; charset=utf-8",
                        filename=download_filename(job, ".bilingual.srt"))


@app.get("/api/jobs/{job_id}/dubbing-report.json")
def dubbing_report(job_id: str):
    get_job(job_id)
    path = DATA / f"{job_id}-dubbing-report.json"
    if not path.is_file():
        raise HTTPException(404, "配音报告尚未生成")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/jobs/{job_id}/voice-clips/{segment_id}.wav")
def voice_clip(job_id: str, segment_id: str):
    """Preview the last generated, individually time-adjusted Chinese voice clip."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", segment_id):
        raise HTTPException(404, "配音句段不存在")
    report = dubbing_report(job_id)
    timing = next((item for item in report.get("timings", []) if item["id"] == segment_id), None)
    if timing is None:
        raise HTTPException(404, "配音句段不存在")
    translation = translation_result(job_id)
    segment = next((item for group in translation["groups"] for item in group["segments"]
                    if item["id"] == segment_id), None)
    if segment is None:
        raise HTTPException(404, "译文句段不存在")
    cache_dir = DATA / "dubbing" / job_id
    config = {"region": report["region"], "voice": report["voice"],
              "engine": report.get("engine", "azure"),
              "hd_temperature": report.get("hd_temperature", AZURE_DEFAULTS["hd_temperature"])}
    rate = float(timing["azure_rate"])
    exact = cache_dir / f"{segment_id}-{clip_cache_key(segment, config, rate)}.wav"
    if exact.is_file() and segment_id not in report.get("stale_segments", []):
        path = exact
    else:
        candidates = list(cache_dir.glob(f"{segment_id}-*.wav"))
        if not candidates:
            raise HTTPException(404, "此句的配音缓存不存在")
        path = max(candidates, key=lambda item: item.stat().st_mtime_ns)
    samples = stretch(read_wav(path.read_bytes()), float(timing["post_speed"]))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.tobytes())
    return Response(output.getvalue(), media_type="audio/wav", headers={"Cache-Control": "no-store"})


class ChineseEdit(BaseModel):
    zh: str


@app.patch("/api/jobs/{job_id}/translation/{segment_id}")
def edit_chinese(job_id: str, segment_id: str, edit: ChineseEdit):
    job = get_job(job_id)
    if job["translation_status"] != "done" or job["dubbing_status"] in ("queued", "running"):
        raise HTTPException(409, "请在翻译完成且配音未运行时修改中文")
    chinese = " ".join(edit.zh.split())
    if len(chinese) > 500:
        raise HTTPException(400, "中文台词不能超过 500 字")
    path = DATA / f"{job_id}-translation.json"
    with db_lock:
        result = json.loads(path.read_text(encoding="utf-8"))
        items = [item for group in result["groups"] for item in group["segments"]]
        target = next((item for item in items if item["id"] == segment_id), None)
        if target is None:
            raise HTTPException(404, "译文句段不存在")
        if target["zh"] == chinese:
            return {"ok": True, "id": segment_id, "zh": chinese, "changed": False}
        target["zh"] = chinese
        target["pronunciations"] = []
        target["user_edited"] = True
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (DATA / f"{job_id}-zh.srt").write_text(make_srt(items), encoding="utf-8-sig")
        (DATA / f"{job_id}-bilingual.srt").write_text(make_bilingual_srt(items), encoding="utf-8-sig")
        report_path = DATA / f"{job_id}-dubbing-report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["stale_segments"] = sorted(set(report.get("stale_segments", [])) | {segment_id})
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        invalidate_dubbing(job_id, preserve_report=True)
    return {"ok": True, "id": segment_id, "zh": chinese, "changed": True}


@app.delete("/api/jobs/{job_id}/translation/{segment_id}")
def drop_translation_segment(job_id: str, segment_id: str):
    job = get_job(job_id)
    if job["translation_status"] != "done" or job["dubbing_status"] in ("queued", "running"):
        raise HTTPException(409, "请在翻译完成且配音未运行时删除句段")
    path = DATA / f"{job_id}-translation.json"
    with db_lock:
        result = json.loads(path.read_text(encoding="utf-8"))
        removed = None
        groups = []
        for group in result["groups"]:
            kept = [item for item in group["segments"] if item["id"] != segment_id]
            if len(kept) != len(group["segments"]):
                removed = next(item for item in group["segments"] if item["id"] == segment_id)
            if kept:
                group = {**group, "segments": kept}
                groups.append(group)
        if removed is None:
            raise HTTPException(404, "译文句段不存在")
        if not groups:
            raise HTTPException(400, "至少保留一个译文句段")
        result["groups"] = groups
        items = [item for group in groups for item in group["segments"]]
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (DATA / f"{job_id}-zh.srt").write_text(make_srt(items), encoding="utf-8-sig")
        (DATA / f"{job_id}-bilingual.srt").write_text(make_bilingual_srt(items), encoding="utf-8-sig")
        report_path = DATA / f"{job_id}-dubbing-report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["issues"] = [item for item in report.get("issues", []) if item.get("id") != segment_id]
            report["stale_segments"] = [item for item in report.get("stale_segments", []) if item != segment_id]
            report["dropped_segments"] = [*report.get("dropped_segments", []),
                                          {"id": segment_id, "en": removed.get("en", ""), "zh": removed.get("zh", "")}]
            report["affected_segments"] = len({item.get("id") or item.get("kind") for item in report["issues"]})
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        invalidate_dubbing(job_id, preserve_report=True)
    return {"ok": True, "id": segment_id}


class BilibiliCookie(BaseModel):
    cookie: str


class BilibiliSettings(BaseModel):
    tid: int
    tags: str


class BilibiliDraft(BaseModel):
    title: str = ""
    desc: str = ""
    tags: str = ""
    dynamic: str = ""
    tid: int | None = None


def _bilibili_view(job_id: str) -> dict:
    return bilibili_provider.load_job_meta(DATA / f"{job_id}-bilibili.json",
                                           bilibili_provider.load_config(BILIBILI_CONFIG))


def _save_bilibili_draft(job_id: str, body: BilibiliDraft, *, require_text: bool) -> dict:
    settings = bilibili_provider.load_config(BILIBILI_CONFIG)
    current = _bilibili_view(job_id)
    title = body.title.strip()[:80]
    desc = body.desc.strip()[:2000]
    if require_text and (not title or not desc):
        raise HTTPException(400, "请先填写中文标题和简介")
    current.update({"title": title, "desc": desc,
                    "tags": bilibili_provider.tag_text(body.tags),
                    "dynamic": str(current.get("dynamic") or "")[:100],
                    "tid": body.tid if body.tid is not None else int(current.get("tid") or settings["tid"])})
    if current["tid"] not in bilibili_provider.PARTITION_TIDS:
        raise HTTPException(400, "请选择支持的投稿分区")
    bilibili_provider.save_job_meta(DATA / f"{job_id}-bilibili.json", current)
    return current


@app.get("/api/bilibili/status")
def bilibili_status():
    try:
        return bilibili_provider.login_status(BILIBILI_COOKIES)
    except Exception as exc:
        return {"logged_in": False, "message": str(exc) or "无法连接 B 站"}


@app.put("/api/bilibili/cookie")
def bilibili_cookie(body: BilibiliCookie):
    try:
        return bilibili_provider.save_cookie_login(BILIBILI_COOKIES, body.cookie)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, str(exc) or "无法验证 B 站 Cookie") from exc


@app.delete("/api/bilibili/cookie")
def bilibili_logout():
    bilibili_provider.clear_cookies(BILIBILI_COOKIES)
    return {"ok": True}


@app.get("/api/bilibili/partitions")
def bilibili_partitions():
    return bilibili_provider.PARTITIONS


@app.get("/api/bilibili/settings")
def bilibili_settings():
    return bilibili_provider.load_config(BILIBILI_CONFIG)


@app.put("/api/bilibili/settings")
def save_bilibili_settings(body: BilibiliSettings):
    try:
        bilibili_provider.save_config(BILIBILI_CONFIG, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return bilibili_provider.load_config(BILIBILI_CONFIG)


@app.get("/api/jobs/{job_id}/bilibili")
def job_bilibili(job_id: str):
    get_job(job_id)
    return _bilibili_view(job_id)


@app.post("/api/jobs/{job_id}/bilibili/meta")
def generate_bilibili_meta(job_id: str):
    job = get_job(job_id)
    if job["translation_status"] != "done":
        raise HTTPException(409, "请先完成中文翻译")
    api_key = get_api_key()
    if not api_key:
        raise HTTPException(400, "请先在设置中保存 DeepSeek API Key")
    subtitle_path = DATA / f"{job_id}-dubbed-zh.srt"
    if not subtitle_path.is_file():
        subtitle_path = DATA / f"{job_id}-zh.srt"
    subtitle_text = bilibili_provider.plain_subtitles(subtitle_path.read_text(encoding="utf-8-sig"))
    meta = bilibili_provider.generate_meta(
        api_key, load_config(DEEPSEEK_CONFIG), filename=job["filename"], subtitle_text=subtitle_text,
        original_title=job.get("title") or "", source_url=job.get("source_url") or "")
    settings = bilibili_provider.load_config(BILIBILI_CONFIG)
    saved = _bilibili_view(job_id)
    saved.update({**meta, "tid": int(saved.get("tid") or settings["tid"]),
                  "tags": meta["tags"] or settings["tags"], "status": "ready",
                  "message": meta.get("warning") or "已生成中文标题和简介", "error": ""})
    bilibili_provider.save_job_meta(DATA / f"{job_id}-bilibili.json", saved)
    return saved


def _auto_publish_bilibili(job_id: str) -> None:
    existing = _bilibili_view(job_id)
    if existing["status"] in ("uploading", "success"):
        return
    generated = generate_bilibili_meta(job_id)
    if generated.get("warning"):
        raise RuntimeError(generated["warning"])
    publish_bilibili(job_id, BilibiliDraft(
        title=generated["title"], desc=generated["desc"], tags=generated["tags"],
        tid=generated["tid"]))


@app.put("/api/jobs/{job_id}/bilibili")
def save_job_bilibili(job_id: str, body: BilibiliDraft):
    get_job(job_id)
    return _save_bilibili_draft(job_id, body, require_text=False)


@app.post("/api/jobs/{job_id}/bilibili/publish")
def publish_bilibili(job_id: str, body: BilibiliDraft):
    job = get_job(job_id)
    if job.get("batch_id"):
        raise HTTPException(409, "批处理子任务不支持 B 站投稿")
    video = DATA / f"{job_id}-dubbed.mp4"
    if not video.is_file():
        raise HTTPException(409, "请先生成配音成片")
    if (DATA / f"{job_id}-manual-edit.json").is_file():
        edit_state = _read_manual_edit(job_id)
        if edit_state["edited_video_available"]:
            video = DATA / f"{job_id}-edited.mp4"
    meta = _save_bilibili_draft(job_id, body, require_text=True)
    source = (job.get("source_url") or "").strip()
    meta["copyright"] = 1
    meta["desc"] = bilibili_provider.append_source_link(meta["desc"], source)
    bilibili_provider.save_job_meta(DATA / f"{job_id}-bilibili.json", meta)
    cover = Path(job["upload_path"]).with_name("cover.jpg") if source else None

    def load_cover():
        if not source or cover is None:
            return None
        if not cover.is_file():
            from ..pipeline.youtube import ensure_youtube_cover
            ensure_youtube_cover(source, cover, YOUTUBE_CONFIG, YOUTUBE_COOKIES)
        return cover if cover.is_file() else None

    try:
        bilibili_provider.start_publish(job_id, video, DATA / f"{job_id}-bilibili.json",
                                        BILIBILI_COOKIES, meta, cover, load_cover if source else None)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _bilibili_view(job_id)
