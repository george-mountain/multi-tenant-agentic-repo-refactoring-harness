
import json

from psycopg_pool import AsyncConnectionPool

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS llm_ledger (
    run_id text NOT NULL,
    node text NOT NULL,
    step_idx integer NOT NULL,
    attempt integer NOT NULL,
    call_idx integer NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    prompt_hash text NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, node, step_idx, attempt, call_idx, provider, model, prompt_hash)
)
"""


class Ledger:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(CREATE_SQL)

    async def get(
        self,
        run_id: str,
        node: str,
        step_idx: int,
        attempt: int,
        call_idx: int,
        provider: str,
        model: str,
        prompt_hash: str,
    ) -> dict | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT response FROM llm_ledger WHERE run_id=%s AND node=%s AND step_idx=%s "
                "AND attempt=%s AND call_idx=%s AND provider=%s AND model=%s AND prompt_hash=%s",
                (run_id, node, step_idx, attempt, call_idx, provider, model, prompt_hash),
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def put(
        self,
        run_id: str,
        node: str,
        step_idx: int,
        attempt: int,
        call_idx: int,
        provider: str,
        model: str,
        prompt_hash: str,
        response: dict,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO llm_ledger VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                (run_id, node, step_idx, attempt, call_idx, provider, model, prompt_hash, json.dumps(response)),
            )
