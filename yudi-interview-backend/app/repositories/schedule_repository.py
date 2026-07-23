from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import InterviewScheduleEntity, InterviewStatus


class ScheduleRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def save(self, entity: InterviewScheduleEntity) -> InterviewScheduleEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

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
    query = select(InterviewScheduleEntity)
    if start is not None and end is not None:
      query = query.where(InterviewScheduleEntity.interview_time.between(start, end))
    elif status is not None:
      query = query.where(InterviewScheduleEntity.status == status.value)
    result = await self.session.execute(
        query.order_by(InterviewScheduleEntity.interview_time.asc())
    )
    return list(result.scalars().all())

  async def list_schedules(
      self,
      page: int,
      page_size: int,
      status: InterviewStatus | None = None,
  ) -> tuple[list[InterviewScheduleEntity], int]:
    query = select(InterviewScheduleEntity)
    count_query = select(func.count(InterviewScheduleEntity.id))
    if status is not None:
      query = query.where(InterviewScheduleEntity.status == status.value)
      count_query = count_query.where(InterviewScheduleEntity.status == status.value)
    total = await self.session.scalar(count_query)
    result = await self.session.execute(
        query.order_by(InterviewScheduleEntity.interview_time.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), int(total or 0)

  async def update(self, entity: InterviewScheduleEntity) -> InterviewScheduleEntity:
    return await self.save(entity)

  async def delete(self, schedule_id: int) -> bool:
    entity = await self.find_by_id(schedule_id)
    if entity is None:
      return False
    await self.session.delete(entity)
    await self.session.flush()
    return True

  async def update_expired(self, cutoff: datetime) -> int:
    # interview_time 所在表为 timestamp without time zone，按北京时间墙上时间比较。
    if cutoff.tzinfo is not None:
      from app.utils.timezone_utils import to_beijing_naive
      cutoff = to_beijing_naive(cutoff)
    result = await self.session.execute(
        update(InterviewScheduleEntity)
        .where(
            InterviewScheduleEntity.status == InterviewStatus.PENDING.value,
            InterviewScheduleEntity.interview_time < cutoff,
        )
        .values(status=InterviewStatus.CANCELLED.value, updated_at=func.now())
    )
    return result.rowcount or 0
