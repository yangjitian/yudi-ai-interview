from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.voice_interview import VoiceInterviewEvaluationEntity


class VoiceEvaluationRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def find_by_session_id(self, session_id: int) -> VoiceInterviewEvaluationEntity | None:
    result = await self.session.execute(
        select(VoiceInterviewEvaluationEntity).where(
            VoiceInterviewEvaluationEntity.session_id == session_id
        )
    )
    return result.scalar_one_or_none()

  async def find_scores_by_session_ids(
      self, session_ids: list[int]
  ) -> dict[int, int | None]:
    """批量查询多个会话的总分，供列表页一次性填充 overallScore。"""
    if not session_ids:
      return {}
    result = await self.session.execute(
        select(
            VoiceInterviewEvaluationEntity.session_id,
            VoiceInterviewEvaluationEntity.overall_score,
        ).where(VoiceInterviewEvaluationEntity.session_id.in_(session_ids))
    )
    return {row.session_id: row.overall_score for row in result}

  async def save(
      self, entity: VoiceInterviewEvaluationEntity
  ) -> VoiceInterviewEvaluationEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def delete(self, entity: VoiceInterviewEvaluationEntity) -> None:
    await self.session.delete(entity)
    await self.session.flush()
