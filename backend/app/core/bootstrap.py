"""Первичная инициализация: администратор при пустой таблице пользователей."""

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import hash_password
from app.models.users import User, UserRole

logger = logging.getLogger(__name__)


async def ensure_admin(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    async with session_factory() as session:
        existing = await session.execute(select(User.id).limit(1))
        if existing.first() is not None:
            return

        password = settings.admin_password or secrets.token_urlsafe(12)
        session.add(
            User(
                username="admin",
                password_hash=hash_password(password),
                full_name="Администратор",
                role=UserRole.ADMIN,
            )
        )
        await session.commit()

    if settings.admin_password:
        logger.info("Created initial admin user 'admin' (password from environment)")
    else:
        # пароль не задан в окружении — единственный способ его узнать
        logger.warning(
            "Created initial admin user 'admin' with GENERATED password: %s "
            "— change it after first login",
            password,
        )
