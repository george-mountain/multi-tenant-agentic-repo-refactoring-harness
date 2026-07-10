
import json
import time

from harness_shared.schemas import EVENT_RUN_STATUS, EVENTS_CHANNEL_ALL, run_channel

from worker import runtime


async def publish(run_id: str, event_type: str, data: dict) -> None:
    """Publish one event to the global channel and the run-scoped channel."""
    seq = await runtime.redis.incr(f"run:{run_id}:seq")
    payload = json.dumps(
        {"run_id": run_id, "seq": seq, "type": event_type, "ts": time.time(), "data": data}
    )
    await runtime.redis.publish(EVENTS_CHANNEL_ALL, payload)
    await runtime.redis.publish(run_channel(run_id), payload)


async def status(run_id: str, value: str, **extra: object) -> None:
    await publish(run_id, EVENT_RUN_STATUS, {"status": value, **extra})


async def log(run_id: str, message: str) -> None:
    await publish(run_id, "log", {"message": message})
