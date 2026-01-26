import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from pathlib import Path
from io import BytesIO

import requests
import redis
from minio import Minio


def env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None or v == "" else v


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return int(v)


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return float(v)


DATA_DIR = env_str("DATA_DIR", "/data")
DB_PATH = env_str("JOBS_DB_PATH", os.path.join(DATA_DIR, "jobs.db"))

REDIS_URL = env_str("REDIS_URL", "redis://redis:6379/0")
QUEUE_STT = env_str("QUEUE_STT", "queue:stt.transcribe")
QUEUE_WHISPER = env_str("QUEUE_WHISPER", "queue:whisper.transcribe")

MINIO_ENDPOINT = env_str("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = env_str("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = env_str("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = env_str("MINIO_SECURE", "false").lower() in ("1", "true", "yes")
MINIO_BUCKET = env_str("MINIO_BUCKET", "artifacts")

WHISPER_URL = env_str("WHISPER_URL", "http://whisper:9000")
WHISPER_MODEL = env_str("WHISPER_MODEL", "base")
WHISPER_LANGUAGE = env_str("WHISPER_LANGUAGE", "auto")
REQUEST_TIMEOUT = float(env_str("REQUEST_TIMEOUT", "300"))  # 5 minutes for large files


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def job_status(job_id: str) -> Optional[str]:
    with db_connect() as conn:
        cur = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        r = cur.fetchone()
        return str(r["status"]) if r else None


def mark_running(job_id: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ? AND status = 'queued'",
            ("running", now_iso(), job_id),
        )
        conn.commit()


def mark_failed(job_id: str, code: str, message: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, error_code = ?, error_message = ? WHERE id = ? AND status != 'cancelled'",
            ("failed", now_iso(), code, message, job_id),
        )
        conn.commit()


def mark_succeeded(job_id: str, bucket: str, object_name: str, content_type: str, bytes_: int) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, finished_at = ?, result_bucket = ?, result_object = ?, result_content_type = ?, result_bytes = ?
            WHERE id = ? AND status != 'cancelled'
            """,
            ("succeeded", now_iso(), bucket, object_name, content_type, bytes_, job_id),
        )
        conn.commit()


def transcribe_with_whisper(audio_file_path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send audio file to Whisper service and return transcription result.
    """
    language = params.get("language") or WHISPER_LANGUAGE
    if language == "auto":
        language = None  # Let Whisper auto-detect
    
    temperature = params.get("temperature")
    if temperature is not None:
        temperature = float(temperature)
    else:
        temperature = 0.0
    
    prompt = params.get("prompt", "")
    
    # Prepare the request to Whisper service
    print(f"[whisper-worker] Opening audio file: {audio_file_path}")
    
    files = {"audio_file": open(audio_file_path, "rb")}
    data = {"task": "transcribe"}
    
    if language:
        data["language"] = language
    if temperature is not None:
        data["temperature"] = temperature  # Keep as float, don't convert to string
    if prompt:
        data["initial_prompt"] = prompt
    
    try:
        print(f"[whisper-worker] Sending request to {WHISPER_URL}/asr with data: {data}")
        response = requests.post(f"{WHISPER_URL}/asr", files=files, data=data, timeout=300.0)
        print(f"[whisper-worker] Whisper response status: {response.status_code}")
        response.raise_for_status()
        
        # The Whisper service returns plain text, so we need to create a JSON structure
        text_result = response.text.strip()
        print(f"[whisper-worker] Whisper text response: {text_result}")
        
        # Create a Whisper-like JSON response
        result = {
            "text": text_result,
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 0.0,  # We don't have timing info from this service
                    "text": text_result
                }
            ],
            "language": language or "en"
        }
        return result
    finally:
        files["audio_file"].close()


def convert_whisper_output(whisper_result: Dict[str, Any], output_format: str) -> tuple[str, str]:
    """
    Convert Whisper JSON result to the requested output format.
    Returns (content, content_type).
    """
    if output_format == "json":
        return json.dumps(whisper_result, indent=2), "application/json"
    
    elif output_format == "text":
        text = whisper_result.get("text", "")
        return text, "text/plain"
    
    elif output_format == "srt":
        # Convert segments to SRT format
        srt_content = ""
        segments = whisper_result.get("segments", [])
        for i, segment in enumerate(segments, 1):
            start = segment.get("start", 0)
            end = segment.get("end", 0)
            text = segment.get("text", "").strip()
            
            # Convert seconds to SRT time format
            start_time = format_srt_time(start)
            end_time = format_srt_time(end)
            
            srt_content += f"{i}\n{start_time} --> {end_time}\n{text}\n\n"
        
        return srt_content, "text/srt"
    
    elif output_format == "vtt":
        # Convert segments to WebVTT format
        vtt_content = "WEBVTT\n\n"
        segments = whisper_result.get("segments", [])
        for segment in segments:
            start = segment.get("start", 0)
            end = segment.get("end", 0)
            text = segment.get("text", "").strip()
            
            # Convert seconds to WebVTT time format
            start_time = format_vtt_time(start)
            end_time = format_vtt_time(end)
            
            vtt_content += f"{start_time} --> {end_time}\n{text}\n\n"
        
        return vtt_content, "text/vtt"
    
    else:
        # Default to JSON if unknown format
        return json.dumps(whisper_result, indent=2), "application/json"


def format_srt_time(seconds: float) -> str:
    """Convert seconds to SRT time format (HH:MM:SS,mmm)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_vtt_time(seconds: float) -> str:
    """Convert seconds to WebVTT time format (HH:MM:SS.mmm)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def process_job(job_id: str, msg: Dict[str, Any], minio_client: Minio) -> None:
    """
    Process a single transcription job.
    """
    try:
        print(f"[whisper-worker] Processing job {job_id}, msg: {msg}")
        input_info = msg.get("input", {})
        params = msg.get("params", {})
        
        input_bucket = input_info.get("bucket")
        input_object = input_info.get("object")
        
        if not input_bucket or not input_object:
            mark_failed(job_id, "invalid_input", "Missing input bucket or object")
            return
        
        output_format = params.get("output_format", "json")
        
        # Download audio file from MinIO
        print(f"[whisper-worker] Downloading {input_bucket}/{input_object}")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".audio") as temp_file:
            try:
                minio_client.fget_object(input_bucket, input_object, temp_file.name)
                print(f"[whisper-worker] Downloaded to {temp_file.name}")
                
                # Transcribe with Whisper
                print(f"[whisper-worker] Transcribing {input_object} for job {job_id}")
                print(f"[whisper-worker] Params: {params}")
                try:
                    whisper_result = transcribe_with_whisper(temp_file.name, params)
                except Exception as e:
                    print(f"[whisper-worker] Exception in transcribe_with_whisper: {e}")
                    import traceback
                    traceback.print_exc()
                    raise
                
                # Convert to requested format
                content, content_type = convert_whisper_output(whisper_result, output_format)
                
                # Upload result to MinIO
                output_object = f"results/{job_id}/transcription.{output_format}"
                content_bytes = content.encode('utf-8')
                
                print(f"[whisper-worker] Uploading result: bucket={MINIO_BUCKET}, object={output_object}, size={len(content_bytes)}")
                minio_client.put_object(
                    MINIO_BUCKET,
                    output_object,
                    data=BytesIO(content_bytes),
                    length=len(content_bytes),
                    content_type=content_type
                )
                
                # Mark job as succeeded
                mark_succeeded(job_id, MINIO_BUCKET, output_object, content_type, len(content_bytes))
                print(f"[whisper-worker] Job {job_id} completed successfully")
                
            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_file.name)
                except OSError:
                    pass
                    
    except requests.HTTPError as e:
        error_msg = f"Whisper service error: {e.response.status_code}"
        if e.response.text:
            error_msg += f" - {e.response.text}"
        mark_failed(job_id, "whisper_service_error", error_msg)
        print(f"[whisper-worker] Job {job_id} failed: {error_msg}")
        
    except Exception as e:
        mark_failed(job_id, "processing_error", str(e))
        print(f"[whisper-worker] Job {job_id} failed with error: {e}")


def main() -> None:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    m = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE)
    if not m.bucket_exists(MINIO_BUCKET):
        m.make_bucket(MINIO_BUCKET)

    # Listen to both STT queue (to replace existing worker) and dedicated Whisper queue
    queues = [QUEUE_STT, QUEUE_WHISPER]
    
    print(f"[whisper-worker] queues={queues} redis={REDIS_URL} whisper={WHISPER_URL} db={DB_PATH}")
    print(f"[whisper-worker] model={WHISPER_MODEL} language={WHISPER_LANGUAGE}")

    while True:
        # Use brpop with multiple queues - it will return from whichever has data first
        item = r.brpop(queues, timeout=5)
        if not item:
            continue
        
        queue_name, raw = item
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[whisper-worker] invalid json message from {queue_name}; skipping")
            continue
        except Exception as e:
            print(f"[whisper-worker] error decoding message from {queue_name}: {e}")
            continue

        job_id = msg.get("job_id")
        if not job_id:
            print(f"[whisper-worker] missing job_id from {queue_name}; skipping")
            continue

        if job_status(job_id) == "cancelled":
            print(f"[whisper-worker] job cancelled; skipping {job_id}")
            continue

        mark_running(job_id)
        try:
            process_job(job_id, msg, m)
        except Exception as e:
            print(f"[whisper-worker] Exception in process_job: {e}")
            import traceback
            traceback.print_exc()
            mark_failed(job_id, "processing_error", str(e))


if __name__ == "__main__":
    main()