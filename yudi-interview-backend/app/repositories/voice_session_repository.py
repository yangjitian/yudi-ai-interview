from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.voice_interview import VoiceInterviewSessionEntity


class VoiceSessionRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def find_by_id(self, session_id: int) -> VoiceInterviewSessionEntity | None:
    result = await self.session.execute(
        select(VoiceInterviewSessionEntity).where(
            VoiceInterviewSessionEntity.id == session_id
        )
    )
    return result.scalar_one_or_none()

  async def find_by_user_id(
      self,
      user_id: str,
      status: str | None = None,
  ) -> list[VoiceInterviewSessionEntity]:
    stmt = select(VoiceInterviewSessionEntity).where(
        VoiceInterviewSessionEntity.user_id == user_id
    )
    if status is not None:
      stmt = stmt.where(VoiceInterviewSessionEntity.status == status)
    stmt = stmt.order_by(VoiceInterviewSessionEntity.updated_at.desc())
    result = await self.session.execute(stmt)
    return list(result.scalars().all())

  async def save(self, entity: VoiceInterviewSessionEntity) -> VoiceInterviewSessionEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def commit(self) -> None:
    await self.session.commit()

  async def find_stale_in_progress(
      self, before: datetime
  ) -> list[VoiceInterviewSessionEntity]:
    result = await self.session.execute(
        select(VoiceInterviewSessionEntity).where(
            VoiceInterviewSessionEntity.status == "IN_PROGRESS",
            VoiceInterviewSessionEntity.start_time < before,
        )
    )
    return list(result.scalars().all())

  async def delete(self, session_id: int) -> bool:
    entity = await self.find_by_id(session_id)
    if entity is None:
      return False
    await self.session.delete(entity)
    await self.session.flush()
    return True

  async def exists_by_id(self, session_id: int) -> bool:
    result = await self.session.execute(
        select(VoiceInterviewSessionEntity.id).where(
            VoiceInterviewSessionEntity.id == session_id
        )
    )
    return result.scalar_one_or_none() is not None
