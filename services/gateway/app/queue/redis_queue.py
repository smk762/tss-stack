import json
from typing import Any, Dict

import redis.asyncio as redis

from app.core import config


class RedisQueue:
    def __init__(self) -> None:
        self._client = redis.from_url(config.REDIS_URL, decode_responses=True)

    async def enqueue(self, queue_name: str, payload: Dict[str, Any]) -> None:
        await self._client.lpush(queue_name, json.dumps(payload))

