import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional
import uuid
from minio.error import S3Error

import redis
from minio import Minio


def env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None or v == "" else v


DATA_DIR = env_str("DATA_DIR", "/data")
DB_PATH = env_str("JOBS_DB_PATH", os.path.join(DATA_DIR, "jobs.db"))

REDIS_URL = env_str("REDIS_URL", "redis://redis:6379/0")
QUEUE_STT = env_str("QUEUE_STT", "queue:stt.transcribe")

MINIO_ENDPOINT = env_str("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = env_str("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = env_str("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = env_str("MINIO_SECURE", "false").lower() in ("1", "true", "yes")
MINIO_BUCKET = env_str("MINIO_BUCKET", "artifacts")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def job_status(job_id: str) -> Optional[str]:
    conn = db_connect()
    try:
        cur = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        r = cur.fetchone()
        return str(r["status"]) if r else None
    finally:
        conn.close()


def mark_running(job_id: str) -> None:
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ? AND status = 'queued'",
            ("running", now_iso(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_failed(job_id: str, code: str, message: str) -> None:
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, error_code = ?, error_message = ? WHERE id = ? AND status != 'cancelled'",
            ("failed", now_iso(), code, message, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    m = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE)
    try:
        if not m.bucket_exists(MINIO_BUCKET):
            m.make_bucket(MINIO_BUCKET)
    except S3Error as e:
        if getattr(e, "code", "") not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise

    print(f"[stt-worker] queue={QUEUE_STT} redis={REDIS_URL} db={DB_PATH}")
    print("[stt-worker] NOTE: STT engine is not implemented yet; jobs will fail with not_implemented.")

    while True:
        item = r.brpop(QUEUE_STT, timeout=5)
        if not item:
            continue
        _, raw = item
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print("[stt-worker] invalid json message; skipping")
            continue

        job_id = msg.get("job_id")
        if not job_id:
            continue
        try:
            uuid.UUID(str(job_id))
        except Exception:
            print("[stt-worker] invalid job_id; skipping")
            continue

        if job_status(job_id) == "cancelled":
            print(f"[stt-worker] job cancelled; skipping {job_id}")
            continue

        mark_running(job_id)
        # Placeholder until an STT engine is selected/added (faster-whisper, whisper.cpp, etc).
        mark_failed(job_id, "not_implemented", "STT worker is scaffolded but no engine is configured yet.")


if __name__ == "__main__":
    main()

