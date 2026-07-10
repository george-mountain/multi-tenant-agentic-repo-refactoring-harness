"""Sandbox orchestrator: the only privileged service.

Spawns one ephemeral sibling container plus one named volume per run and
exposes a small HTTP tool API. Write endpoints enforce a per-stage
allowlist so the Planner stage physically cannot mutate files.
"""

import io
import logging
import os
import shlex
import tarfile

import docker
from docker.errors import APIError, NotFound
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sandbox-orchestrator")

SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "harness-sandbox:latest")
SANDBOX_CPU = float(os.environ.get("SANDBOX_CPU", "1.0"))
SANDBOX_MEM = os.environ.get("SANDBOX_MEM", "2g")
EGRESS_NETWORK = os.environ.get("SANDBOX_EGRESS_NETWORK", "harness-sandbox-egress")
WORKSPACE = "/workspace"
OUTPUT_LIMIT = 65536

app = FastAPI(title="sandbox-orchestrator")
client = docker.from_env()

WRITE_STAGES = {"executor", "system"}
ALL_STAGES = {"planner", "executor", "system"}


class CreateRequest(BaseModel):
    run_id: str


class ExecRequest(BaseModel):
    cmd: str
    timeout: int = Field(default=600, le=1800)
    workdir: str = WORKSPACE
    env: dict[str, str] = Field(default_factory=dict)


class ReadRequest(BaseModel):
    path: str


class WriteRequest(BaseModel):
    path: str
    content_b64: str


class SearchReplaceRequest(BaseModel):
    path: str
    old: str
    new: str
    replace_all: bool = False


class AstEditRequest(BaseModel):
    path: str
    lang: str
    pattern: str
    rewrite: str


class NetworkRequest(BaseModel):
    connected: bool


def container_name(run_id: str) -> str:
    return f"sbx-{run_id}"


def volume_name(run_id: str) -> str:
    return f"sbxvol-{run_id}"


def get_container(run_id: str):
    try:
        return client.containers.get(container_name(run_id))
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=f"sandbox for run {run_id} not found") from exc


def require_stage(stage: str, write: bool) -> None:
    if stage not in ALL_STAGES:
        raise HTTPException(status_code=400, detail=f"unknown stage {stage!r}")
    if write and stage not in WRITE_STAGES:
        raise HTTPException(status_code=403, detail=f"stage {stage!r} is not allowed to mutate files")


def safe_path(path: str) -> str:
    normalized = os.path.normpath(os.path.join(WORKSPACE, path.lstrip("/")))
    if normalized != WORKSPACE and not normalized.startswith(WORKSPACE + "/"):
        raise HTTPException(status_code=400, detail=f"path {path!r} escapes the workspace")
    return normalized


def read_bytes(container, path: str) -> bytes:
    try:
        stream, _ = container.get_archive(path)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=f"file {path!r} not found") from exc
    buf = io.BytesIO()
    for chunk in stream:
        buf.write(chunk)
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tar:
        member = next(m for m in tar.getmembers() if m.isfile())
        extracted = tar.extractfile(member)
        if extracted is None:
            raise HTTPException(status_code=500, detail=f"could not extract {path!r}")
        return extracted.read()


def write_bytes(container, path: str, data: bytes) -> None:
    directory, filename = os.path.split(path)
    container.exec_run(["mkdir", "-p", directory], user="runner")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        info.uid = 1000
        info.gid = 1000
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    ok = container.put_archive(directory, buf.getvalue())
    if not ok:
        raise HTTPException(status_code=500, detail=f"failed to write {path!r}")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/sandboxes")
def create_sandbox(req: CreateRequest) -> dict[str, str]:
    name = container_name(req.run_id)
    try:
        existing = client.containers.get(name)
        return {"run_id": req.run_id, "status": existing.status}
    except NotFound:
        pass
    volume = client.volumes.create(name=volume_name(req.run_id))
    try:
        client.containers.run(
            SANDBOX_IMAGE,
            command=["sleep", "infinity"],
            name=name,
            detach=True,
            network=EGRESS_NETWORK,
            volumes={volume.name: {"bind": WORKSPACE, "mode": "rw"}},
            working_dir=WORKSPACE,
            mem_limit=SANDBOX_MEM,
            nano_cpus=int(SANDBOX_CPU * 1e9),
            pids_limit=512,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            labels={"harness.run_id": req.run_id},
        )
    except APIError as exc:
        raise HTTPException(status_code=500, detail=f"failed to start sandbox: {exc.explanation}") from exc
    log.info("created sandbox for run %s", req.run_id)
    return {"run_id": req.run_id, "status": "running"}


@app.get("/sandboxes/{run_id}")
def sandbox_status(run_id: str) -> dict[str, str]:
    container = get_container(run_id)
    return {"run_id": run_id, "status": container.status}


@app.delete("/sandboxes/{run_id}")
def destroy_sandbox(run_id: str) -> dict[str, str]:
    try:
        container = client.containers.get(container_name(run_id))
        container.remove(force=True)
    except NotFound:
        pass
    try:
        client.volumes.get(volume_name(run_id)).remove(force=True)
    except NotFound:
        pass
    log.info("destroyed sandbox for run %s", run_id)
    return {"run_id": run_id, "status": "destroyed"}


@app.post("/sandboxes/{run_id}/network")
def set_network(run_id: str, req: NetworkRequest) -> dict[str, str]:
    container = get_container(run_id)
    network = client.networks.get(EGRESS_NETWORK)
    attached = container_name(run_id) in [c.name for c in network.containers]
    try:
        if req.connected and not attached:
            network.connect(container)
        elif not req.connected and attached:
            network.disconnect(container)
    except APIError as exc:
        raise HTTPException(status_code=500, detail=f"network change failed: {exc.explanation}") from exc
    return {"run_id": run_id, "connected": str(req.connected).lower()}


@app.post("/sandboxes/{run_id}/exec")
def exec_command(
    run_id: str,
    req: ExecRequest,
    x_agent_stage: str = Header(default="system"),
) -> dict[str, object]:
    require_stage(x_agent_stage, write=False)
    container = get_container(run_id)
    wrapped = ["timeout", str(req.timeout), "sh", "-lc", req.cmd]
    result = container.exec_run(
        wrapped,
        workdir=req.workdir,
        environment=req.env,
        demux=True,
        user="runner",
    )
    stdout_raw, stderr_raw = result.output if result.output else (b"", b"")
    stdout = (stdout_raw or b"")[-OUTPUT_LIMIT:].decode("utf-8", errors="replace")
    stderr = (stderr_raw or b"")[-OUTPUT_LIMIT:].decode("utf-8", errors="replace")
    return {"exit_code": result.exit_code, "stdout": stdout, "stderr": stderr}


@app.post("/sandboxes/{run_id}/files/read")
def read_file(
    run_id: str,
    req: ReadRequest,
    x_agent_stage: str = Header(default="system"),
) -> dict[str, str]:
    require_stage(x_agent_stage, write=False)
    import base64

    container = get_container(run_id)
    data = read_bytes(container, safe_path(req.path))
    return {"path": req.path, "content_b64": base64.b64encode(data).decode("ascii")}


@app.post("/sandboxes/{run_id}/files/write")
def write_file(
    run_id: str,
    req: WriteRequest,
    x_agent_stage: str = Header(default="system"),
) -> dict[str, str]:
    require_stage(x_agent_stage, write=True)
    import base64

    container = get_container(run_id)
    write_bytes(container, safe_path(req.path), base64.b64decode(req.content_b64))
    return {"path": req.path, "status": "written"}


@app.post("/sandboxes/{run_id}/files/search_replace")
def search_replace(
    run_id: str,
    req: SearchReplaceRequest,
    x_agent_stage: str = Header(default="system"),
) -> dict[str, object]:
    require_stage(x_agent_stage, write=True)
    container = get_container(run_id)
    path = safe_path(req.path)
    content = read_bytes(container, path).decode("utf-8", errors="replace")
    occurrences = content.count(req.old)
    if occurrences == 0:
        raise HTTPException(status_code=409, detail="old text not found; re-read the file and provide an exact match")
    if occurrences > 1 and not req.replace_all:
        raise HTTPException(
            status_code=409,
            detail=f"old text matches {occurrences} times; add surrounding context or set replace_all",
        )
    updated = content.replace(req.old, req.new) if req.replace_all else content.replace(req.old, req.new, 1)
    write_bytes(container, path, updated.encode("utf-8"))
    return {"path": req.path, "replacements": occurrences if req.replace_all else 1}


@app.post("/sandboxes/{run_id}/files/ast_edit")
def ast_edit(
    run_id: str,
    req: AstEditRequest,
    x_agent_stage: str = Header(default="system"),
) -> dict[str, object]:
    require_stage(x_agent_stage, write=True)
    container = get_container(run_id)
    path = safe_path(req.path)
    cmd = (
        f"ast-grep run --pattern {shlex.quote(req.pattern)} "
        f"--rewrite {shlex.quote(req.rewrite)} --lang {shlex.quote(req.lang)} "
        f"--update-all {shlex.quote(path)}"
    )
    result = container.exec_run(["sh", "-lc", cmd], workdir=WORKSPACE, demux=True, user="runner")
    stdout_raw, stderr_raw = result.output if result.output else (b"", b"")
    return {
        "exit_code": result.exit_code,
        "stdout": (stdout_raw or b"")[-OUTPUT_LIMIT:].decode("utf-8", errors="replace"),
        "stderr": (stderr_raw or b"")[-OUTPUT_LIMIT:].decode("utf-8", errors="replace"),
    }
