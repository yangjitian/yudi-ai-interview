from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import InterviewScheduleEntity, InterviewStatus


class ScheduleRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def find_by_id(self, schedule_id: int) -> InterviewScheduleEntity | None:
    result = await self.session.execute(
        select(InterviewScheduleEntity).where(InterviewScheduleEntity.id == schedule_id)
    )
    return result.scalar_one_or_none()

  async def list_all(
      self,
      status: InterviewStatus | None = None,
      start: datetime | None = None,
      end: datetime | None = None,
  ) -> list[InterviewScheduleEntity]:
    stmt = select(InterviewScheduleEntity)
    if status is not None:
      stmt = stmt.where(InterviewScheduleEntity.status == status.value)
    if start is not None:
      stmt = stmt.where(InterviewScheduleEntity.interview_time >= start)
    if end is not None:
      stmt = stmt.where(InterviewScheduleEntity.interview_time <= end)
    stmt = stmt.order_by(InterviewScheduleEntity.interview_time.asc())
    result = await self.session.execute(stmt)
    return list(result.scalars().all())

  async def save(self, entity: InterviewScheduleEntity) -> InterviewScheduleEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def update(self, entity: InterviewScheduleEntity) -> InterviewScheduleEntity:
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def delete(self, schedule_id: int) -> bool:
    entity = await self.find_by_id(schedule_id)
    if entity is None:
      return False
    await self.session.delete(entity)
    await self.session.flush()
    return True

  async def update_expired(self, cutoff: datetime) -> int:
    """将面试时间在 cutoff 之前且状态为 PENDING 的记录标记为 CANCELLED。"""
    stmt = (
        update(InterviewScheduleEntity)
        .where(
            InterviewScheduleEntity.status == InterviewStatus.PENDING.value,
            InterviewScheduleEntity.interview_time < cutoff,
        )
        .values(status=InterviewStatus.CANCELLED.value)
    )
    result = await self.session.execute(stmt)
    await self.session.flush()
    return result.rowcount
