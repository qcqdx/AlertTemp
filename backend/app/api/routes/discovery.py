from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin, require_viewer
from app.api.schemas import DiscoveredTopicOut
from app.core.db import get_session
from app.models import DiscoveredTopic, DiscoveredTopicStatus

router = APIRouter(
    prefix="/api/v1/discovery", tags=["discovery"], dependencies=[Depends(require_viewer)]
)


@router.get("", response_model=list[DiscoveredTopicOut])
async def list_discovered(
    status: DiscoveredTopicStatus | None = DiscoveredTopicStatus.NEW,
    session: AsyncSession = Depends(get_session),
) -> list[DiscoveredTopic]:
    query = select(DiscoveredTopic).order_by(DiscoveredTopic.last_seen.desc())
    if status is not None:
        query = query.where(DiscoveredTopic.status == status)
    result = await session.execute(query)
    return list(result.scalars().all())


async def _get_discovered(session: AsyncSession, discovered_id: int) -> DiscoveredTopic:
    row = await session.get(DiscoveredTopic, discovered_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Discovered topic not found")
    return row


@router.post(
    "/{discovered_id}/ignore",
    response_model=DiscoveredTopicOut,
    dependencies=[Depends(require_admin)],
)
async def ignore_discovered(
    discovered_id: int, session: AsyncSession = Depends(get_session)
) -> DiscoveredTopic:
    row = await _get_discovered(session, discovered_id)
    if row.status == DiscoveredTopicStatus.BOUND:
        raise HTTPException(status_code=409, detail="Topic is bound to a sensor")
    row.status = DiscoveredTopicStatus.IGNORED
    await session.commit()
    return row


@router.post(
    "/{discovered_id}/restore",
    response_model=DiscoveredTopicOut,
    dependencies=[Depends(require_admin)],
)
async def restore_discovered(
    discovered_id: int, session: AsyncSession = Depends(get_session)
) -> DiscoveredTopic:
    row = await _get_discovered(session, discovered_id)
    if row.status == DiscoveredTopicStatus.BOUND:
        raise HTTPException(status_code=409, detail="Topic is bound to a sensor")
    row.status = DiscoveredTopicStatus.NEW
    await session.commit()
    return row
