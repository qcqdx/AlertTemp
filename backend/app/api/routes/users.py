from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.core.db import get_session
from app.core.security import drop_user_sessions, hash_password
from app.models.audit import record_audit
from app.models.users import User, UserRole

router = APIRouter(
    prefix="/api/v1/users", tags=["users"], dependencies=[Depends(require_admin)]
)


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    full_name: str = Field(min_length=1, max_length=200)
    role: UserRole = UserRole.VIEWER


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: UserRole | None = None
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: str
    role: UserRole
    enabled: bool


@router.get("", response_model=list[UserOut])
async def list_users(session: AsyncSession = Depends(get_session)) -> list[User]:
    rows = await session.execute(select(User).order_by(User.username))
    return list(rows.scalars().all())


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreate,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> User:
    duplicate = await session.execute(select(User.id).where(User.username == body.username))
    if duplicate.first() is not None:
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role=body.role,
    )
    session.add(user)
    await session.flush()
    record_audit(
        session, admin.username, "create", "user", user.id, f"{body.username} ({body.role})"
    )
    await session.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UserUpdate,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    updates = body.model_dump(exclude_unset=True)
    if updates.get("enabled") is False and user.id == admin.id:
        raise HTTPException(status_code=409, detail="Cannot disable your own account")
    if "role" in updates and user.id == admin.id and updates["role"] != UserRole.ADMIN:
        raise HTTPException(status_code=409, detail="Cannot demote your own account")

    password = updates.pop("password", None)
    if password:
        user.password_hash = hash_password(password)
        await drop_user_sessions(session, user.id)
    for attr, value in updates.items():
        setattr(user, attr, value)
    if updates.get("enabled") is False:
        await drop_user_sessions(session, user.id)
    audit_detail = {k: v for k, v in updates.items() if k != "password"}
    if password:
        audit_detail["password"] = "changed"
    record_audit(session, admin.username, "update", "user", user_id, str(audit_detail))
    await session.commit()
    return user
