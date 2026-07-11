from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_session
from app.core.security import (
    SESSION_COOKIE,
    create_session,
    drop_session,
    drop_user_sessions,
    hash_password,
    verify_password,
)
from app.models.audit import record_audit
from app.models.users import User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class MeOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=get_settings().session_cookie_secure,
        max_age=7 * 24 * 3600,
    )


@router.post("/login", response_model=MeOut)
async def login(
    body: LoginRequest, response: Response, session: AsyncSession = Depends(get_session)
) -> MeOut:
    row = await session.execute(select(User).where(User.username == body.username))
    user = row.scalar_one_or_none()
    # verify выполняется и для несуществующего пользователя — не даём
    # отличить «нет такого логина» от «неверный пароль» по времени ответа
    password_hash = user.password_hash if user else hash_password("invalid")
    if not verify_password(password_hash, body.password) or user is None or not user.enabled:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = await create_session(session, user.id)
    record_audit(session, user.username, "login", "session")
    await session.commit()
    _set_cookie(response, token)
    return MeOut(id=user.id, username=user.username, full_name=user.full_name, role=user.role)


@router.post("/logout", status_code=204)
async def logout(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await drop_session(session, token)
        await session.commit()
    response.delete_cookie(SESSION_COOKIE)


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut(id=user.id, username=user.username, full_name=user.full_name, role=user.role)


@router.post("/password", status_code=204)
async def change_password(
    body: PasswordChange,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    managed = await session.get(User, user.id)
    managed.password_hash = hash_password(body.new_password)
    # все сессии отзываются; текущую переустанавливаем, чтобы не разлогинить
    await drop_user_sessions(session, user.id)
    token = await create_session(session, user.id)
    await session.commit()
    _set_cookie(response, token)
