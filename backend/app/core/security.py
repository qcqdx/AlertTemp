"""Пароли (argon2) и серверные сессии."""

import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.users import User, UserSession

SESSION_COOKIE = "coldwatch_session"
SESSION_LIFETIME = timedelta(days=7)

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError:
        return False


async def create_session(session: AsyncSession, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session.add(
        UserSession(
            token=token, user_id=user_id, created_at=now, expires_at=now + SESSION_LIFETIME
        )
    )
    return token


async def resolve_session(session: AsyncSession, token: str) -> User | None:
    row = await session.execute(
        select(User)
        .join(UserSession, UserSession.user_id == User.id)
        .where(
            UserSession.token == token,
            UserSession.expires_at > datetime.now(UTC),
            User.enabled.is_(True),
        )
    )
    return row.scalar_one_or_none()


async def drop_session(session: AsyncSession, token: str) -> None:
    await session.execute(delete(UserSession).where(UserSession.token == token))


async def drop_user_sessions(session: AsyncSession, user_id: int) -> None:
    """Смена пароля / блокировка пользователя отзывают все его сессии."""
    await session.execute(delete(UserSession).where(UserSession.user_id == user_id))
