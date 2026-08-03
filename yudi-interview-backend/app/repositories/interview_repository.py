from typing import Optional, Sequence
import json

from sqlalchemy import Index, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.interview import InterviewAnswerEntity, InterviewSessionEntity


class InterviewRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def find_by_id(self, session_id: int) -> InterviewSessionEntity | None:
    result = await self.session.execute(
        select(InterviewSessionEntity).where(InterviewSessionEntity.id == session_id)
    )
    return result.scalar_one_or_none()

  async def find_by_session_id(self, session_id: str) -> InterviewSessionEntity | None:
    result = await self.session.execute(
        select(InterviewSessionEntity).where(InterviewSessionEntity.session_id == session_id)
    )
    return result.scalar_one_or_none()

  async def find_answers_by_session_id(
      self, session_id: str
  ) -> list[InterviewAnswerEntity]:
    result = await self.session.execute(
        select(InterviewAnswerEntity)
        .join(
            InterviewSessionEntity,
            InterviewAnswerEntity.session_id == InterviewSessionEntity.id,
        )
        .where(InterviewSessionEntity.session_id == session_id)
        .order_by(InterviewAnswerEntity.question_index)
    )
    return list(result.scalars().all())

  async def find_unfinished_session(
      self, resume_id: int
  ) -> InterviewSessionEntity | None:
    result = await self.session.execute(
        select(InterviewSessionEntity)
        .where(
            InterviewSessionEntity.resume_id == resume_id,
            InterviewSessionEntity.status.in_(["CREATED", "IN_PROGRESS"]),
        )
        .order_by(InterviewSessionEntity.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

  async def list_sessions(
      self, page: int = 1, page_size: int = 20
  ) -> tuple[list[InterviewSessionEntity], int]:
    offset = (page - 1) * page_size
    total_count = func.count(InterviewSessionEntity.id).over().label("total_count")
    result = await self.session.execute(
        select(InterviewSessionEntity, total_count)
        .order_by(InterviewSessionEntity.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = result.all()
    if not rows:
      return [], 0
    return [row[0] for row in rows], rows[0][1]

  async def save(self, entity: InterviewSessionEntity) -> InterviewSessionEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def delete(self, session_id: str) -> bool:
    entity = await self.find_by_session_id(session_id)
    if entity is None:
      return False
    await self.session.delete(entity)
    await self.session.flush()
    return True

  async def delete_by_resume_id(self, resume_id: int) -> None:
    result = await self.session.execute(
        select(InterviewSessionEntity).where(
            InterviewSessionEntity.resume_id == resume_id
        )
    )
    for entity in result.scalars().all():
      await self.session.delete(entity)
    await self.session.flush()

  async def update_status(
      self, session_id: str, status: str
  ) -> None:
    entity = await self.find_by_session_id(session_id)
    if entity:
      entity.status = status

  async def update_evaluate_status(
      self, session_id: str, status: str, error: str | None = None
  ) -> None:
    entity = await self.find_by_session_id(session_id)
    if entity:
      entity.evaluate_status = status
      entity.evaluate_error = error


class InterviewAnswerRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def find_by_session(self, session_id: int) -> list[InterviewAnswerEntity]:
    result = await self.session.execute(
        select(InterviewAnswerEntity)
        .where(InterviewAnswerEntity.session_id == session_id)
        .order_by(InterviewAnswerEntity.question_index)
    )
    return list(result.scalars().all())

  async def find_answer(
      self, session_id: int, question_index: int
  ) -> InterviewAnswerEntity | None:
    result = await self.session.execute(
        select(InterviewAnswerEntity)
        .where(
            InterviewAnswerEntity.session_id == session_id,
            InterviewAnswerEntity.question_index == question_index,
        )
    )
    return result.scalar_one_or_none()

  async def save(self, entity: InterviewAnswerEntity) -> InterviewAnswerEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def upsert(
      self,
      session_id: int,
      question_index: int,
      question: str,
      category: str,
      user_answer: str,
  ) -> InterviewAnswerEntity:
    existing = await self.find_answer(session_id, question_index)
    if existing:
      existing.user_answer = user_answer
      await self.session.flush()
      return existing
    entity = InterviewAnswerEntity(
        session_id=session_id,
        question_index=question_index,
        question=question,
        category=category,
        user_answer=user_answer,
    )
    return await self.save(entity)

  async def update_evaluation(
      self,
      session_id: int,
      question_index: int,
      score: int,
      feedback: str,
      reference_answer: str | None,
      key_points_json: str | None,
      eval_status: str,
  ) -> None:
    existing = await self.find_answer(session_id, question_index)
    if existing is None:
      return
    existing.score = score
    existing.feedback = feedback
    existing.reference_answer = reference_answer
    existing.key_points_json = key_points_json
    existing.eval_status = eval_status
    await self.session.flush()
