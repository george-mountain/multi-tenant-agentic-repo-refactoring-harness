
import json

import redis.asyncio as aioredis
from harness_shared.schemas import JOB_STREAM

from app.config import settings

redis_client = aioredis.from_url(
    settings.redis_url, decode_responses=True, socket_timeout=None, socket_connect_timeout=10
)


async def enqueue_run(payload: dict) -> str:
    """Append a run job to the stream; workers consume via a consumer group."""
    return await redis_client.xadd(JOB_STREAM, {"payload": json.dumps(payload)})
