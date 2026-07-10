
import asyncio
import contextlib
import logging

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.consumer import consume_events
from app.db import engine
from app.models import Base
from app.routers import auth, runs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    consumer_task = asyncio.create_task(consume_events())
    yield
    consumer_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer_task


app = FastAPI(title="refactor-harness-backend", lifespan=lifespan)
app.include_router(auth.router, prefix="/api")
app.include_router(runs.router, prefix="/api")

Instrumentator().instrument(app).expose(app, endpoint="/metrics")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
