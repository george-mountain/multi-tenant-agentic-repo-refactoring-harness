
from dataclasses import dataclass

import jwt as pyjwt
from fastapi import Depends, HTTPException, Query, Request

from app.security import decode_token


@dataclass
class Principal:
    user_id: str
    tenant_id: str


def _decode_or_401(token: str) -> Principal:
    try:
        payload = decode_token(token)
    except pyjwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc
    return Principal(user_id=payload["sub"], tenant_id=payload["tenant_id"])


async def current_principal(
    request: Request,
    token: str | None = Query(default=None),
) -> Principal:
    """Accept a Bearer header or, for EventSource clients, a `token` query parameter."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return _decode_or_401(header[7:])
    if token:
        return _decode_or_401(token)
    raise HTTPException(status_code=401, detail="missing credentials")


CurrentPrincipal = Depends(current_principal)
