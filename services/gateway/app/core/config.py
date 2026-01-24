import os


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


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


API_VERSION = "v1"

# Jobs / storage
DATA_DIR = env_str("DATA_DIR", "/data")
RESULT_URL_TTL_SECONDS = env_int("RESULT_URL_TTL_SECONDS", 900)
IDEMPOTENCY_TTL_SECONDS = env_int("IDEMPOTENCY_TTL_SECONDS", 60)

# Redis queue
REDIS_URL = env_str("REDIS_URL", "redis://redis:6379/0")
QUEUE_STT = env_str("QUEUE_STT", "queue:stt.transcribe")
QUEUE_TTS = env_str("QUEUE_TTS", "queue:tts.synthesize")

# MinIO
MINIO_ENDPOINT = env_str("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = env_str("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = env_str("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = env_bool("MINIO_SECURE", False)
MINIO_BUCKET = env_str("MINIO_BUCKET", "artifacts")
MINIO_REGION = env_str("MINIO_REGION", "us-east-1")

# Presigned URL host (important for local dev):
# - Gateway talks to MinIO on the Docker network (MINIO_ENDPOINT)
# - Clients fetch artifacts from the host via the published port
#   so the presigned URL must be signed for that host:port.
MINIO_PRESIGN_ENDPOINT = env_str("MINIO_PRESIGN_ENDPOINT", MINIO_ENDPOINT)
MINIO_PRESIGN_SECURE = env_bool("MINIO_PRESIGN_SECURE", MINIO_SECURE)

# STT limits
STT_MAX_BYTES = env_int("STT_MAX_BYTES", 50 * 1024 * 1024)
STT_SUPPORTED_MIME_TYPES = [
    s.strip()
    for s in env_str(
        "STT_SUPPORTED_MIME_TYPES",
        "audio/wav,audio/x-wav,audio/mpeg,audio/mp3,audio/webm,audio/ogg",
    ).split(",")
    if s.strip()
]

# TTS
TTS_OUTPUT_FORMATS = [s.strip() for s in env_str("TTS_OUTPUT_FORMATS", "wav,mp3,flac").split(",") if s.strip()]
STT_OUTPUT_FORMATS = [s.strip() for s in env_str("STT_OUTPUT_FORMATS", "json,text,srt,vtt").split(",") if s.strip()]

