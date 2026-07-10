
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Tenant, User
from app.security import create_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    tenant_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    token: str
    tenant_id: str
    tenant_name: str
    email: str


@router.post("/register", response_model=TokenResponse)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    existing_user = await session.scalar(select(User).where(User.email == body.email))
    if existing_user:
        raise HTTPException(status_code=409, detail="email already registered")
    existing_tenant = await session.scalar(select(Tenant).where(Tenant.name == body.tenant_name))
    if existing_tenant:
        raise HTTPException(status_code=409, detail="tenant name already taken")

    tenant = Tenant(name=body.tenant_name)
    session.add(tenant)
    await session.flush()
    user = User(tenant_id=tenant.id, email=body.email, password_hash=hash_password(body.password))
    session.add(user)
    await session.commit()
    return TokenResponse(
        token=create_token(user.id, tenant.id),
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        email=user.email,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = await session.scalar(select(User).where(User.email == body.email))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=500, detail="tenant record missing")
    return TokenResponse(
        token=create_token(user.id, user.tenant_id),
        tenant_id=user.tenant_id,
        tenant_name=tenant.name,
        email=user.email,
    )
