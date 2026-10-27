"""SQLite persistence for jobs and recognized subtitle segments."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException


@contextmanager
def connect(database: Path):
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize(open_connection):
    with open_connection() as conn:
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
        conn.execute("""CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, source_dir TEXT NOT NULL,
            created_at TEXT NOT NULL)""")
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
            "separate_vocals": "INTEGER NOT NULL DEFAULT 1",
            "source_url": "TEXT NOT NULL DEFAULT ''",
            "publish_to_bilibili": "INTEGER NOT NULL DEFAULT 0",
            "dubbing_engine": "TEXT NOT NULL DEFAULT 'azure'",
            "batch_id": "TEXT",
            "batch_source_path": "TEXT",
            "pause_requested": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        conn.execute("""UPDATE jobs SET status='paused',
                     translation_status=CASE WHEN translation_status IN ('queued','correcting','glossary','running')
                         THEN 'paused' ELSE translation_status END,
                     dubbing_status=CASE WHEN dubbing_status IN ('queued','running')
                         THEN 'paused' ELSE dubbing_status END
                     WHERE pause_requested=1""")
        conn.execute("UPDATE jobs SET status='interrupted', error='服务重启中断了处理；可点击重试' WHERE status IN ('queued','running')")
        conn.execute("UPDATE jobs SET translation_status='interrupted', translation_error='服务重启中断了翻译；可重试' WHERE translation_status IN ('queued','correcting','glossary','running')")
        conn.execute("UPDATE jobs SET dubbing_status='interrupted', dubbing_error='服务重启中断了配音；可重试' WHERE dubbing_status IN ('queued','running')")
        conn.execute("UPDATE jobs SET translation_error=? WHERE translation_error LIKE '%latin-1%codec%'",
                     ("已保存的 DeepSeek API Key 含非 ASCII 字符，请在左侧重新粘贴平台生成的密钥后重试",))


def get_job(open_connection, job_id: str) -> dict:
    with open_connection() as conn:
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


def update_job(open_connection, lock, data: Path, job_id: str, **fields):
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    with lock, open_connection() as conn:
        conn.execute("UPDATE jobs SET " + ", ".join(f"{key}=?" for key in fields)
                     + " WHERE id=?", (*fields.values(), job_id))
    events = []
    if "stage" in fields:
        events.append(f"阶段：{fields['stage']}")
    for name, title in (("status", "处理"), ("translation_status", "翻译"),
                        ("dubbing_status", "配音")):
        if name in fields:
            events.append(f"{title}状态：{fields[name]}")
    for name in ("error", "translation_error", "dubbing_error"):
        if fields.get(name):
            events.append(f"原因：{fields[name]}")
    if events:
        with (data / f"{job_id}.log").open("a", encoding="utf-8") as output:
            output.write(f"\n[{datetime.now().astimezone().strftime('%H:%M:%S')}] {'；'.join(events)}\n")


def segment_rows(open_connection, job_id: str) -> list[dict]:
    with open_connection() as conn:
        rows = conn.execute("SELECT * FROM segments WHERE job_id=? ORDER BY start,id", (job_id,)).fetchall()
    return [{**dict(row), "words": json.loads(row["words"]), "flags": json.loads(row["flags"]),
             "reviewed": bool(row["reviewed"]), "needs_realign": bool(row["needs_realign"])} for row in rows]


def save_segments(open_connection, lock, job_id: str, segments: list[dict]):
    with lock, open_connection() as conn:
        conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
        conn.executemany("""INSERT INTO segments
            (job_id,id,start,end,raw_text,edited_text,words,flags,reviewed,needs_realign)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", [
            (job_id, s["id"], s["start"], s["end"], s["raw_text"], s["edited_text"],
             json.dumps(s["words"]), json.dumps(s["flags"]), int(s["reviewed"]),
             int(s["needs_realign"])) for s in segments])
