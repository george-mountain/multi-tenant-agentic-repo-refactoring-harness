
import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from harness_shared.schemas import run_channel
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts import delete_run_artifacts, get_artifact
from app.config import settings
from app.db import SessionFactory, get_session
from app.deps import CurrentPrincipal, Principal
from app.models import Run, RunEvent
from app.queue import enqueue_run, redis_client

router = APIRouter(prefix="/runs", tags=["runs"])

ACTIVE_STATUSES = ("queued", "ingesting", "planning", "executing", "verifying", "finalizing")


class CreateRunRequest(BaseModel):
    """Only the repository URL is required; everything else is discovered by the agent."""

    repo_url: str = Field(pattern=r"^https://(www\.)?github\.com/[\w.\-]+/[\w.\-]+/?$")
    base_branch: str = ""
    objective: str = ""
    provider: str = Field(default="", pattern=r"^(openai|gemini)?$")
    model: str = ""
    test_cmd: str = ""
    lint_cmd: str = ""
    approval_required: bool = False


class RunResponse(BaseModel):
    id: str
    repo_url: str
    base_branch: str
    objective: str
    provider: str
    model: str
    status: str
    branch: str
    pr_url: str
    failure: str
    approval_required: bool
    plan: dict | None
    tokens: dict | None
    created_at: str

    @classmethod
    def from_row(cls, run: Run) -> "RunResponse":
        return cls(
            id=run.id,
            repo_url=run.repo_url,
            base_branch=run.base_branch,
            objective=run.objective,
            provider=run.provider,
            model=run.model,
            status=run.status,
            branch=run.branch,
            pr_url=run.pr_url,
            failure=run.failure,
            approval_required=run.approval_required,
            plan=run.plan,
            tokens=run.tokens,
            created_at=run.created_at.isoformat(),
        )


async def _get_owned_run(run_id: str, principal: Principal, session: AsyncSession) -> Run:
    run = await session.get(Run, run_id)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("", response_model=RunResponse, status_code=201)
async def create_run(
    body: CreateRunRequest,
    principal: Principal = CurrentPrincipal,
    session: AsyncSession = Depends(get_session),
) -> RunResponse:
    active = await session.scalar(
        select(func.count(Run.id)).where(Run.tenant_id == principal.tenant_id, Run.status.in_(ACTIVE_STATUSES))
    )
    if (active or 0) >= settings.max_concurrent_runs_per_tenant:
        raise HTTPException(
            status_code=429,
            detail=f"tenant concurrency cap reached ({settings.max_concurrent_runs_per_tenant} active runs)",
        )

    run = Run(
        tenant_id=principal.tenant_id,
        created_by=principal.user_id,
        repo_url=body.repo_url.rstrip("/"),
        base_branch=body.base_branch,
        objective=body.objective,
        provider=body.provider or settings.default_llm_provider,
        model=body.model,
        test_cmd=body.test_cmd,
        lint_cmd=body.lint_cmd,
        approval_required=body.approval_required,
    )
    session.add(run)
    await session.commit()
    await _enqueue(run)
    return RunResponse.from_row(run)


async def _enqueue(run: Run) -> None:
    await enqueue_run(
        {
            "run_id": run.id,
            "tenant_id": run.tenant_id,
            "action": "run",
            "repo_url": run.repo_url,
            "base_branch": run.base_branch,
            "objective": run.objective,
            "provider": run.provider,
            "model": run.model,
            "test_cmd": run.test_cmd,
            "lint_cmd": run.lint_cmd,
            "approval_required": run.approval_required,
        }
    )


class RunListResponse(BaseModel):
    items: list[RunResponse]
    total: int
    limit: int
    offset: int


class RunStats(BaseModel):
    total: int
    active: int
    succeeded: int
    failed: int


def _status_filter(query, status: str):
    if status == "active":
        return query.where(Run.status.in_(ACTIVE_STATUSES))
    if status == "succeeded":
        return query.where(Run.status == "succeeded")
    if status == "failed":
        return query.where(Run.status == "failed")
    return query


@router.get("", response_model=RunListResponse)
async def list_runs(
    principal: Principal = CurrentPrincipal,
    q: str = Query(default=""),
    status: Literal["all", "active", "succeeded", "failed"] = Query(default="all"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> RunListResponse:
    base = select(Run).where(Run.tenant_id == principal.tenant_id)
    base = _status_filter(base, status)
    if q.strip():
        term = f"%{q.strip()}%"
        base = base.where(or_(Run.repo_url.ilike(term), Run.objective.ilike(term)))

    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = await session.scalars(
        base.order_by(Run.created_at.desc()).limit(limit).offset(offset)
    )
    return RunListResponse(
        items=[RunResponse.from_row(run) for run in rows], total=total, limit=limit, offset=offset
    )


@router.get("/stats", response_model=RunStats)
async def run_stats(
    principal: Principal = CurrentPrincipal,
    session: AsyncSession = Depends(get_session),
) -> RunStats:
    def count_for(condition=None):
        query = select(func.count(Run.id)).where(Run.tenant_id == principal.tenant_id)
        return query.where(condition) if condition is not None else query

    total = await session.scalar(count_for()) or 0
    active = await session.scalar(count_for(Run.status.in_(ACTIVE_STATUSES))) or 0
    succeeded = await session.scalar(count_for(Run.status == "succeeded")) or 0
    failed = await session.scalar(count_for(Run.status == "failed")) or 0
    return RunStats(total=total, active=active, succeeded=succeeded, failed=failed)


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    principal: Principal = CurrentPrincipal,
    session: AsyncSession = Depends(get_session),
) -> RunResponse:
    run = await _get_owned_run(run_id, principal, session)
    return RunResponse.from_row(run)


@router.delete("/{run_id}", status_code=204)
async def delete_run(
    run_id: str,
    principal: Principal = CurrentPrincipal,
    session: AsyncSession = Depends(get_session),
) -> None:
    run = await _get_owned_run(run_id, principal, session)
    if run.status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="cannot delete a run that is still in progress")
    await session.execute(delete(RunEvent).where(RunEvent.run_id == run.id))
    await session.delete(run)
    await session.commit()
    await asyncio.to_thread(delete_run_artifacts, run.id)


@router.post("/{run_id}/retry", response_model=RunResponse)
async def retry_run(
    run_id: str,
    principal: Principal = CurrentPrincipal,
    session: AsyncSession = Depends(get_session),
) -> RunResponse:
    original = await _get_owned_run(run_id, principal, session)
    if original.status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="run is still in progress")
    run = Run(
        tenant_id=principal.tenant_id,
        created_by=principal.user_id,
        repo_url=original.repo_url,
        base_branch=original.base_branch,
        objective="",
        provider=original.provider,
        model=original.model,
        test_cmd=original.test_cmd,
        lint_cmd=original.lint_cmd,
        approval_required=original.approval_required,
    )
    session.add(run)
    await session.commit()
    await _enqueue(run)
    return RunResponse.from_row(run)


@router.post("/{run_id}/approve", response_model=RunResponse)
async def approve_plan(
    run_id: str,
    principal: Principal = CurrentPrincipal,
    session: AsyncSession = Depends(get_session),
) -> RunResponse:
    run = await _get_owned_run(run_id, principal, session)
    if run.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail=f"run is {run.status}, not awaiting approval")
    await enqueue_run({"run_id": run.id, "tenant_id": run.tenant_id, "action": "resume"})
    run.status = "executing"
    await session.commit()
    return RunResponse.from_row(run)


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    principal: Principal = CurrentPrincipal,
    session: AsyncSession = Depends(get_session),
):
    run = await _get_owned_run(run_id, principal, session)

    async def generate():
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(run_channel(run.id))
        try:
            async with SessionFactory() as replay_session:
                replayed = await replay_session.scalars(
                    select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
                )
                history = [
                    {"run_id": run.id, "seq": e.seq, "type": e.type, "ts": e.ts, "data": e.data}
                    for e in replayed
                ]
            last_seq = 0
            for event in history:
                last_seq = max(last_seq, event["seq"])
                yield f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n"
            if any(event["type"] in ("run_completed", "run_failed") for event in history):
                return
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                if message is None:
                    yield ": heartbeat\n\n"
                    continue
                event = json.loads(message["data"])
                if event["seq"] <= last_seq:
                    continue
                last_seq = event["seq"]
                yield f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n"
                if event["type"] in ("run_completed", "run_failed"):
                    break
        finally:
            await pubsub.unsubscribe(run_channel(run.id))
            await pubsub.close()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/{run_id}/steps/{step_idx}/diff", response_class=PlainTextResponse)
async def step_diff(
    run_id: str,
    step_idx: int,
    principal: Principal = CurrentPrincipal,
    session: AsyncSession = Depends(get_session),
) -> str:
    run = await _get_owned_run(run_id, principal, session)
    return await asyncio.to_thread(get_artifact, f"runs/{run.id}/steps/{step_idx}.diff")


@router.get("/{run_id}/patch", response_class=PlainTextResponse)
async def final_patch(
    run_id: str,
    principal: Principal = CurrentPrincipal,
    session: AsyncSession = Depends(get_session),
) -> str:
    run = await _get_owned_run(run_id, principal, session)
    return await asyncio.to_thread(get_artifact, f"runs/{run.id}/final.patch")


@router.get("/{run_id}/report")
async def failure_report(
    run_id: str,
    principal: Principal = CurrentPrincipal,
    session: AsyncSession = Depends(get_session),
) -> dict:
    run = await _get_owned_run(run_id, principal, session)
    raw = await asyncio.to_thread(get_artifact, f"runs/{run.id}/report.json")
    return json.loads(raw)
