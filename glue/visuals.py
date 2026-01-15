# glue/visuals.py
from __future__ import annotations
import os, asyncio, logging
from typing import Iterable, Optional
import httpx

log = logging.getLogger("glue.visuals")

# Map audio target → Argus visual webhook
VISUAL_TARGETS = {
    "argus": {
        "url": os.getenv("ARGUS_VISUAL_URL", "http://argus:5055/visuals/play"),
        "token": os.getenv("ARGUS_VISUAL_TOKEN", "supersecretchangeme"),
        "media": os.getenv("ARGUS_VISUAL_MEDIA", "/opt/argus-visual/space_girl.mp4"),
        "loop": True,
    },
}

async def _post_one(url: str, token: str, media: str, duration_ms: int, text: str, loop: bool):
    payload = {
        "media": media,
        "duration_ms": max(0, int(duration_ms)),
        "text": text,
        "loop": loop,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Argus-Token": token,
    }
    timeout = httpx.Timeout(connect=1.0, read=2.5, write=2.5, pool=1.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            await client.post(url, json=payload, headers=headers)
        except Exception as e:
            log.warning("visual webhook failed: %s %s", url, e)

def _schedule_post_one(url: str, token: str, media: str, duration_ms: int, text: str, loop: bool):
    """Sync wrapper for FastAPI BackgroundTasks."""
    try:
        loop_ = asyncio.get_running_loop()
        loop_.create_task(_post_one(url, token, media, duration_ms, text, loop))
    except RuntimeError:
        # No running loop (unlikely here), just run it to completion.
        asyncio.run(_post_one(url, token, media, duration_ms, text, loop))

async def notify_visuals(
    targets: Iterable[str],
    text: str,
    duration_ms: int,
    media_override: Optional[str] = None,
    loop_override: Optional[bool] = None,
    background_tasks=None,  # FastAPI BackgroundTasks or None
):
    """Fire Argus visual webhooks for any matching audio targets."""
    for t in targets or []:
        cfg = VISUAL_TARGETS.get(t)
        if not cfg:
            continue
        url   = cfg["url"]
        token = cfg.get("token", "")
        media = media_override or cfg["media"]
        loop  = cfg["loop"] if loop_override is None else bool(loop_override)

        if background_tasks is not None:
            # Don’t create a coroutine here—schedule it inside the sync wrapper
            background_tasks.add_task(_schedule_post_one, url, token, media, duration_ms, text, loop)
        else:
            # Fire-and-forget in the current event loop
            asyncio.create_task(_post_one(url, token, media, duration_ms, text, loop))
