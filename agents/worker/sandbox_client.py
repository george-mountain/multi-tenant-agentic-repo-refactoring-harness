
import base64

import httpx


class SandboxError(RuntimeError):
    """Raised when a sandbox operation fails."""


class ExecResult:
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SandboxClient:
    """Talks to the orchestrator; every call carries the agent stage for allowlisting."""

    def __init__(self, base_url: str) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(1900.0, connect=10.0))

    async def close(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, payload: dict, stage: str) -> dict:
        response = await self._http.post(path, json=payload, headers={"X-Agent-Stage": stage})
        if response.status_code >= 400:
            detail = response.json().get("detail", response.text) if response.content else response.text
            raise SandboxError(f"{path}: {detail}")
        return response.json()

    async def create(self, run_id: str) -> None:
        await self._post("/sandboxes", {"run_id": run_id}, "system")

    async def exists(self, run_id: str) -> bool:
        response = await self._http.get(f"/sandboxes/{run_id}")
        return response.status_code == 200 and response.json().get("status") == "running"

    async def destroy(self, run_id: str) -> None:
        await self._http.delete(f"/sandboxes/{run_id}")

    async def set_network(self, run_id: str, connected: bool) -> None:
        await self._post(f"/sandboxes/{run_id}/network", {"connected": connected}, "system")

    async def exec(
        self,
        run_id: str,
        cmd: str,
        stage: str = "system",
        timeout: int = 600,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        data = await self._post(
            f"/sandboxes/{run_id}/exec",
            {"cmd": cmd, "timeout": timeout, "workdir": workdir, "env": env or {}},
            stage,
        )
        return ExecResult(data["exit_code"], data["stdout"], data["stderr"])

    async def read_file(self, run_id: str, path: str, stage: str = "system") -> str:
        data = await self._post(f"/sandboxes/{run_id}/files/read", {"path": path}, stage)
        return base64.b64decode(data["content_b64"]).decode("utf-8", errors="replace")

    async def read_file_bytes(self, run_id: str, path: str) -> bytes:
        data = await self._post(f"/sandboxes/{run_id}/files/read", {"path": path}, "system")
        return base64.b64decode(data["content_b64"])

    async def write_file(self, run_id: str, path: str, content: str, stage: str = "executor") -> None:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        await self._post(f"/sandboxes/{run_id}/files/write", {"path": path, "content_b64": encoded}, stage)

    async def write_file_bytes(self, run_id: str, path: str, content: bytes) -> None:
        encoded = base64.b64encode(content).decode("ascii")
        await self._post(f"/sandboxes/{run_id}/files/write", {"path": path, "content_b64": encoded}, "system")

    async def search_replace(
        self, run_id: str, path: str, old: str, new: str, replace_all: bool = False
    ) -> dict:
        return await self._post(
            f"/sandboxes/{run_id}/files/search_replace",
            {"path": path, "old": old, "new": new, "replace_all": replace_all},
            "executor",
        )

    async def ast_edit(self, run_id: str, path: str, lang: str, pattern: str, rewrite: str) -> dict:
        return await self._post(
            f"/sandboxes/{run_id}/files/ast_edit",
            {"path": path, "lang": lang, "pattern": pattern, "rewrite": rewrite},
            "executor",
        )
