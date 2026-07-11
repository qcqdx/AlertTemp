"""Аутентификация запросов и проверка ролей."""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import SESSION_COOKIE, resolve_session
from app.models.users import User, UserRole


async def get_current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = await resolve_session(session, token)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired")
    return user


def require_role(*roles: UserRole):
    """Админ проходит любую проверку; остальные — по списку ролей."""

    async def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role != UserRole.ADMIN and user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user

    return dependency


# любой аутентифицированный (viewer и выше)
require_viewer = require_role(UserRole.VIEWER, UserRole.OPERATOR)
# оператор и выше: подтверждение инцидентов, комментарии
require_operator = require_role(UserRole.OPERATOR)
# только администратор: устройства, пороги, получатели, пользователи
require_admin = require_role()
