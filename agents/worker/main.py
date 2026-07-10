
import asyncio
import json
import logging
import os
import socket

import redis.asyncio as aioredis
from harness_shared.schemas import EVENT_RUN_FAILED, JOB_GROUP, JOB_STREAM
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from prometheus_client import Counter, start_http_server
from psycopg_pool import AsyncConnectionPool

from worker import events, runtime
from worker.config import settings
from worker.graph import build_graph
from worker.ledger import Ledger
from worker.sandbox_client import SandboxClient
from worker.storage import Storage, snapshot_key, step_bundle_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

RUNS_STARTED = Counter("harness_runs_started_total", "Run jobs picked up")
RUNS_SUCCEEDED = Counter("harness_runs_succeeded_total", "Runs finished successfully")
RUNS_FAILED = Counter("harness_runs_failed_total", "Runs that ended in failure")

CONSUMER = f"{socket.gethostname()}-{os.getpid()}"
CLAIM_MIN_IDLE_MS = 10 * 60 * 1000
HEARTBEAT_SECONDS = 60


def _setup_langfuse() -> list:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return []
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except ImportError:
        log.warning("langfuse keys set but SDK import failed; tracing disabled")
        return []


async def restore_sandbox(state: dict) -> None:
    """Rebuild a dead sandbox from the MinIO snapshot / latest step bundle."""
    run_id = state["run_id"]
    await runtime.sandbox.create(run_id)
    committed = state.get("committed_steps") or []
    key = step_bundle_key(run_id, committed[-1]) if committed else snapshot_key(run_id)
    if not runtime.storage.exists(key):
        key = snapshot_key(run_id)
    bundle = runtime.storage.get(key)
    await runtime.sandbox.write_file_bytes(run_id, "restore.bundle", bundle)
    branch = state.get("branch") or "main"
    clone = await runtime.sandbox.exec(
        run_id, f"rm -rf repo && git clone -b {branch} restore.bundle repo && rm restore.bundle", timeout=600
    )
    if clone.exit_code != 0:
        raise RuntimeError(f"sandbox restore failed: {clone.stderr[-2000:]}")
    tree = (await runtime.sandbox.exec(run_id, "git ls-files", workdir="/workspace/repo")).stdout.splitlines()
    await runtime.sandbox.set_network(run_id, True)
    try:
        from worker.nodes import _install_dependencies

        await _install_dependencies(run_id, tree)
    finally:
        await runtime.sandbox.set_network(run_id, False)
    log.info("restored sandbox for run %s from %s", run_id, key)


async def handle_job(graph, payload: dict, callbacks: list) -> None:
    run_id = payload["run_id"]
    action = payload.get("action", "run")
    config = {"configurable": {"thread_id": run_id}, "callbacks": callbacks, "recursion_limit": 1000}

    snapshot = await graph.aget_state(config)
    has_checkpoint = bool(snapshot and snapshot.values)

    if has_checkpoint and not snapshot.next:
        log.info("run %s already finished; ignoring duplicate delivery", run_id)
        return

    if has_checkpoint and snapshot.values.get("base_sha"):
        if not await runtime.sandbox.exists(run_id):
            await restore_sandbox(snapshot.values)

    if action == "resume" or (has_checkpoint and snapshot.next):
        graph_input = Command(resume=True) if action == "resume" else None
    else:
        graph_input = {
            "run_id": run_id,
            "tenant_id": payload["tenant_id"],
            "provider": payload.get("provider") or settings.default_llm_provider,
            "model": payload.get("model") or "",
            "repo_url": payload["repo_url"],
            "base_branch": payload.get("base_branch") or "",
            "objective": payload.get("objective") or "",
            "test_cmd": payload.get("test_cmd") or "",
            "lint_cmd": payload.get("lint_cmd") or "",
            "approval_required": bool(payload.get("approval_required")),
            "tokens": {"input_tokens": 0, "output_tokens": 0},
        }

    RUNS_STARTED.inc()
    result = await graph.ainvoke(graph_input, config)
    if result.get("__interrupt__"):
        log.info("run %s paused awaiting approval", run_id)
        return
    if result.get("failure"):
        RUNS_FAILED.inc()
    else:
        RUNS_SUCCEEDED.inc()


async def heartbeat(redis_client, message_id: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            await redis_client.xclaim(
                JOB_STREAM, JOB_GROUP, CONSUMER, min_idle_time=0, message_ids=[message_id], justid=True
            )
        except aioredis.RedisError:
            log.warning("heartbeat xclaim failed for %s", message_id)


async def consume(graph, callbacks: list) -> None:
    redis_client = runtime.redis
    try:
        await redis_client.xgroup_create(JOB_STREAM, JOB_GROUP, id="0", mkstream=True)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    while True:
        try:
            claimed = await redis_client.xautoclaim(
                JOB_STREAM, JOB_GROUP, CONSUMER, min_idle_time=CLAIM_MIN_IDLE_MS, start_id="0-0", count=1
            )
            entries = claimed[1] if claimed else []
            if not entries:
                fresh = await redis_client.xreadgroup(
                    JOB_GROUP, CONSUMER, {JOB_STREAM: ">"}, count=1, block=5000
                )
                entries = fresh[0][1] if fresh else []
        except aioredis.RedisError:
            log.warning("redis unavailable while polling; retrying in 3s")
            await asyncio.sleep(3)
            continue
        for message_id, fields in entries:
            payload = json.loads(fields[b"payload"] if b"payload" in fields else fields["payload"])
            log.info("picked up run %s (%s)", payload.get("run_id"), payload.get("action", "run"))
            beat = asyncio.create_task(heartbeat(redis_client, message_id))
            try:
                await handle_job(graph, payload, callbacks)
            except Exception as exc:
                log.exception("run %s crashed in worker", payload.get("run_id"))
                RUNS_FAILED.inc()
                try:
                    await events.publish(payload["run_id"], EVENT_RUN_FAILED, {"failure": str(exc)[:2000]})
                    await events.status(payload["run_id"], "failed", failure=str(exc)[:2000])
                    await runtime.sandbox.destroy(payload["run_id"])
                except Exception:
                    log.exception("failed to publish failure event / clean up sandbox")
            finally:
                beat.cancel()
                await redis_client.xack(JOB_STREAM, JOB_GROUP, message_id)


async def main() -> None:
    start_http_server(settings.metrics_port)
    runtime.redis = aioredis.from_url(
        settings.redis_url, decode_responses=False, socket_timeout=None, socket_connect_timeout=10
    )
    runtime.sandbox = SandboxClient(settings.sandbox_api_url)
    runtime.storage = Storage(
        settings.minio_endpoint, settings.minio_root_user, settings.minio_root_password, settings.minio_bucket
    )
    runtime.storage.ensure_bucket()

    pool = AsyncConnectionPool(settings.agent_database_url, min_size=1, max_size=5, open=False)
    await pool.open()
    runtime.ledger = Ledger(pool)
    await runtime.ledger.setup()

    callbacks = _setup_langfuse()

    async with AsyncPostgresSaver.from_conn_string(settings.agent_database_url) as saver:
        await saver.setup()
        graph = build_graph(saver)
        log.info("worker %s ready, consuming %s", CONSUMER, JOB_STREAM)
        await consume(graph, callbacks)


if __name__ == "__main__":
    asyncio.run(main())
