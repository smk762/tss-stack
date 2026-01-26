from __future__ import annotations

import io
from datetime import timedelta
from typing import Optional, Any

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

    def get_object(self, bucket: str, object_name: str) -> Any:
        """
        Return a streaming object response from MinIO.

        Callers MUST close the returned object (and release the connection) when done:
        - resp.close()
        - resp.release_conn()
        """
        return self.client_internal.get_object(bucket, object_name)

    def get_object_content(self, bucket: str, object_name: str) -> Optional[str]:
        """
        Get the content of an object as a string.
        Returns None if the object doesn't exist or can't be read.
        """
        try:
            response = self.client_internal.get_object(bucket, object_name)
            try:
                content = response.read().decode('utf-8')
                return content
            finally:
                response.close()
                response.release_conn()
        except Exception:
            return None

    def guess_ext(self, mime: Optional[str]) -> str:
        if not mime:
            return "bin"
        m = mime.lower().strip()
        if "wav" in m:
            return "wav"
        if "mpeg" in m or "mp3" in m:
            return "mp3"
        if "flac" in m:
            return "flac"
        if "webm" in m:
            return "webm"
        if "ogg" in m:
            return "ogg"
        return "bin"

