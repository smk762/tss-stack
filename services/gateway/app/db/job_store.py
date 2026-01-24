import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from app.core import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class JobRow:
    id: str
    type: str
    status: str
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    progress: Optional[float]
    error_code: Optional[str]
    error_message: Optional[str]
    error_details_json: Optional[str]
    result_bucket: Optional[str]
    result_object: Optional[str]
    result_content_type: Optional[str]
    result_bytes: Optional[int]
    result_sha256: Optional[str]
    owner_id: Optional[str]


class JobStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        self.db_path = db_path or os.path.join(config.DATA_DIR, "jobs.db")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,
                  type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT,
                  progress REAL,
                  error_code TEXT,
                  error_message TEXT,
                  error_details_json TEXT,
                  result_bucket TEXT,
                  result_object TEXT,
                  result_content_type TEXT,
                  result_bytes INTEGER,
                  result_sha256 TEXT,
                  owner_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency (
                  idem_key TEXT PRIMARY KEY,
                  job_id TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner_id)")
            conn.commit()

    def create_job(self, job_id: str, job_type: str, owner_id: Optional[str]) -> JobRow:
        row = JobRow(
            id=job_id,
            type=job_type,
            status="queued",
            created_at=_now_iso(),
            started_at=None,
            finished_at=None,
            progress=0.0,
            error_code=None,
            error_message=None,
            error_details_json=None,
            result_bucket=None,
            result_object=None,
            result_content_type=None,
            result_bytes=None,
            result_sha256=None,
            owner_id=owner_id,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                  id,type,status,created_at,started_at,finished_at,progress,
                  error_code,error_message,error_details_json,
                  result_bucket,result_object,result_content_type,result_bytes,result_sha256,
                  owner_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.id,
                    row.type,
                    row.status,
                    row.created_at,
                    row.started_at,
                    row.finished_at,
                    row.progress,
                    row.error_code,
                    row.error_message,
                    row.error_details_json,
                    row.result_bucket,
                    row.result_object,
                    row.result_content_type,
                    row.result_bytes,
                    row.result_sha256,
                    row.owner_id,
                ),
            )
            conn.commit()
        return row

    def get_job(self, job_id: str) -> Optional[JobRow]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            r = cur.fetchone()
        return self._row(r) if r else None

    def cancel_job(self, job_id: str) -> Optional[JobRow]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            r = cur.fetchone()
            if not r:
                return None
            status = r["status"]
            if status in ("succeeded", "failed", "cancelled"):
                return self._row(r)
            conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                ("cancelled", _now_iso(), job_id),
            )
            conn.commit()
            cur2 = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            return self._row(cur2.fetchone())

    def mark_running(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ? AND status = 'queued'",
                ("running", _now_iso(), job_id),
            )
            conn.commit()

    def mark_failed(self, job_id: str, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, error_code = ?, error_message = ?, error_details_json = ?
                WHERE id = ? AND status != 'cancelled'
                """,
                ("failed", _now_iso(), code, message, json.dumps(details or {}), job_id),
            )
            conn.commit()

    def mark_succeeded_result(
        self,
        job_id: str,
        bucket: str,
        object_name: str,
        content_type: str,
        bytes_: Optional[int] = None,
        sha256: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, result_bucket = ?, result_object = ?, result_content_type = ?, result_bytes = ?, result_sha256 = ?
                WHERE id = ? AND status != 'cancelled'
                """,
                ("succeeded", _now_iso(), bucket, object_name, content_type, bytes_, sha256, job_id),
            )
            conn.commit()

    def get_or_create_idempotency(self, idem_key: str, owner_id: Optional[str], job_type: str) -> Optional[str]:
        """
        Best-effort idempotency: maps a key to a job id for a short TTL window.
        Key namespace includes owner and job type to avoid cross-user collisions in dev.
        """
        if not idem_key:
            return None
        namespaced = f"{owner_id or 'anon'}:{job_type}:{idem_key}"
        now = _now_iso()
        with self._connect() as conn:
            # purge old keys
            conn.execute(
                "DELETE FROM idempotency WHERE created_at < datetime('now', ?)",
                (f"-{config.IDEMPOTENCY_TTL_SECONDS} seconds",),
            )
            cur = conn.execute("SELECT job_id FROM idempotency WHERE idem_key = ?", (namespaced,))
            r = cur.fetchone()
            if r:
                return str(r["job_id"])
            return None

    def set_idempotency(self, idem_key: str, owner_id: Optional[str], job_type: str, job_id: str) -> None:
        if not idem_key:
            return
        namespaced = f"{owner_id or 'anon'}:{job_type}:{idem_key}"
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO idempotency (idem_key, job_id, created_at) VALUES (?,?,?)",
                (namespaced, job_id, _now_iso()),
            )
            conn.commit()

    def _row(self, r: sqlite3.Row) -> JobRow:
        return JobRow(
            id=str(r["id"]),
            type=str(r["type"]),
            status=str(r["status"]),
            created_at=str(r["created_at"]),
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            progress=r["progress"],
            error_code=r["error_code"],
            error_message=r["error_message"],
            error_details_json=r["error_details_json"],
            result_bucket=r["result_bucket"],
            result_object=r["result_object"],
            result_content_type=r["result_content_type"],
            result_bytes=r["result_bytes"],
            result_sha256=r["result_sha256"],
            owner_id=r["owner_id"],
        )

