
import asyncio
import json
import logging

from harness_shared.schemas import (
    EVENT_PLAN,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_STATUS,
    EVENT_TOKEN_USAGE,
    EVENTS_CHANNEL_ALL,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import SessionFactory
from app.models import Run, RunEvent
from app.queue import redis_client

log = logging.getLogger("backend.consumer")


async def _apply_event(event: dict) -> None:
    async with SessionFactory() as session:
        statement = (
            pg_insert(RunEvent)
            .values(
                run_id=event["run_id"],
                seq=event["seq"],
                type=event["type"],
                data=event["data"],
                ts=event["ts"],
            )
            .on_conflict_do_nothing(index_elements=["run_id", "seq"])
        )
        await session.execute(statement)

        run = await session.get(Run, event["run_id"])
        if run is not None:
            data = event["data"]
            if event["type"] == EVENT_RUN_STATUS:
                run.status = data["status"]
                if data.get("pr_url"):
                    run.pr_url = data["pr_url"]
                if data.get("failure"):
                    run.failure = data["failure"]
                if not run.branch and data["status"] not in ("queued",):
                    run.branch = f"refactor/{run.id[:8]}"
            elif event["type"] == EVENT_PLAN:
                run.plan = data["plan"]
                if not run.objective:
                    run.objective = data["plan"].get("objective", "")
            elif event["type"] == EVENT_TOKEN_USAGE:
                totals = dict(run.tokens or {"input_tokens": 0, "output_tokens": 0})
                totals["input_tokens"] = data.get("input_tokens", totals["input_tokens"])
                totals["output_tokens"] = data.get("output_tokens", totals["output_tokens"])
                run.tokens = totals
            elif event["type"] == EVENT_RUN_COMPLETED and data.get("pr_url"):
                run.pr_url = data["pr_url"]
        await session.commit()


async def consume_events() -> None:
    """Long-lived task started on app startup; reconnects on Redis errors."""
    while True:
        try:
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(EVENTS_CHANNEL_ALL)
            log.info("event consumer subscribed to %s", EVENTS_CHANNEL_ALL)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    await _apply_event(json.loads(message["data"]))
                except Exception:
                    log.exception("failed to apply event: %s", message["data"][:500])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event consumer crashed; reconnecting in 3s")
            await asyncio.sleep(3)
