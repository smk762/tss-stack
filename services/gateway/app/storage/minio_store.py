from __future__ import annotations

import io
from datetime import timedelta
from typing import Optional

from minio import Minio

from app.core import config


class MinioStore:
    def __init__(self) -> None:
        self.client_internal = Minio(
            endpoint=config.MINIO_ENDPOINT,
            access_key=config.MINIO_ACCESS_KEY,
            secret_key=config.MINIO_SECRET_KEY,
            secure=config.MINIO_SECURE,
            region=config.MINIO_REGION,
        )
        # Used only to generate presigned URLs that are valid for the host clients will use.
        self.client_presign = Minio(
            endpoint=config.MINIO_PRESIGN_ENDPOINT,
            access_key=config.MINIO_ACCESS_KEY,
            secret_key=config.MINIO_SECRET_KEY,
            secure=config.MINIO_PRESIGN_SECURE,
            region=config.MINIO_REGION,
        )

    def ensure_bucket(self) -> None:
        if not self.client_internal.bucket_exists(config.MINIO_BUCKET):
            self.client_internal.make_bucket(config.MINIO_BUCKET)

    def put_bytes(self, object_name: str, data: bytes, content_type: str) -> int:
        bio = io.BytesIO(data)
        self.client_internal.put_object(
            bucket_name=config.MINIO_BUCKET,
            object_name=object_name,
            data=bio,
            length=len(data),
            content_type=content_type,
        )
        return len(data)

    def presign_get(self, bucket: str, object_name: str, ttl_seconds: int) -> str:
        return self.client_presign.presigned_get_object(bucket, object_name, expires=timedelta(seconds=ttl_seconds))

    def guess_ext(self, mime: Optional[str]) -> str:
        if not mime:
            return "bin"
        m = mime.lower().strip()
        if "wav" in m:
            return "wav"
        if "mpeg" in m or "mp3" in m:
            return "mp3"
        if "webm" in m:
            return "webm"
        if "ogg" in m:
            return "ogg"
        return "bin"

