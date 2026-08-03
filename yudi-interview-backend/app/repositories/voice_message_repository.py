from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.voice_interview import VoiceInterviewMessageEntity


class VoiceMessageRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def find_by_session_id(self, session_id: int) -> list[VoiceInterviewMessageEntity]:
    result = await self.session.execute(
        select(VoiceInterviewMessageEntity)
        .where(VoiceInterviewMessageEntity.session_id == session_id)
        .order_by(VoiceInterviewMessageEntity.sequence_num.asc())
    )
    return list(result.scalars().all())

  async def count_by_session_id(self, session_id: int) -> int:
    result = await self.session.execute(
        select(func.count(VoiceInterviewMessageEntity.id)).where(
            VoiceInterviewMessageEntity.session_id == session_id
        )
    )
    return result.scalar_one()

  async def find_latest_unanswered_question(
      self, session_id: int
  ) -> VoiceInterviewMessageEntity | None:
    result = await self.session.execute(
        select(VoiceInterviewMessageEntity)
        .where(
            VoiceInterviewMessageEntity.session_id == session_id,
            VoiceInterviewMessageEntity.user_recognized_text.is_(None),
            VoiceInterviewMessageEntity.ai_generated_text.is_not(None),
        )
        .order_by(VoiceInterviewMessageEntity.sequence_num.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

  async def save(
      self, entity: VoiceInterviewMessageEntity
  ) -> VoiceInterviewMessageEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def delete_by_session_id(self, session_id: int) -> None:
    await self.session.execute(
        delete(VoiceInterviewMessageEntity).where(
            VoiceInterviewMessageEntity.session_id == session_id
        )
    )
    await self.session.flush()
