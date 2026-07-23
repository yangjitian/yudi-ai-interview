from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.resume import ResumeAnalysisEntity, ResumeEntity


class ResumeRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def find_by_id(self, resume_id: int) -> ResumeEntity | None:
    result = await self.session.execute(
        select(ResumeEntity).where(ResumeEntity.id == resume_id)
    )
    return result.scalar_one_or_none()

  async def find_by_hash(self, file_hash: str) -> ResumeEntity | None:
    result = await self.session.execute(
        select(ResumeEntity).where(ResumeEntity.file_hash == file_hash)
    )
    return result.scalar_one_or_none()

  async def exists_by_hash(self, file_hash: str) -> bool:
    result = await self.session.execute(
        select(ResumeEntity.file_hash).where(ResumeEntity.file_hash == file_hash)
    )
    return result.scalar_one_or_none() is not None

  async def save(self, entity: ResumeEntity) -> ResumeEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def delete(self, resume_id: int) -> bool:
    entity = await self.find_by_id(resume_id)
    if entity is None:
      return False
    await self.session.delete(entity)
    await self.session.flush()
    return True

  async def list_resumes(
      self, page: int = 1, page_size: int = 20
  ) -> tuple[list[ResumeEntity], int]:
    offset = (page - 1) * page_size
    count_result = await self.session.execute(
        select(func.count(ResumeEntity.id))
    )
    total = count_result.scalar_one()

    result = await self.session.execute(
        select(ResumeEntity)
        .order_by(ResumeEntity.uploaded_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    items = list(result.scalars().all())
    return items, total

  async def update_access_count(self, resume_id: int) -> None:
    entity = await self.find_by_id(resume_id)
    if entity:
      from app.utils.timezone_utils import get_beijing_now
      entity.access_count = (entity.access_count or 0) + 1
      entity.last_accessed_at = get_beijing_now()


class ResumeAnalysisRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def find_by_id(self, analysis_id: int) -> ResumeAnalysisEntity | None:
    result = await self.session.execute(
        select(ResumeAnalysisEntity).where(ResumeAnalysisEntity.id == analysis_id)
    )
    return result.scalar_one_or_none()

  async def find_latest_by_resume_id(
      self, resume_id: int
  ) -> ResumeAnalysisEntity | None:
    result = await self.session.execute(
        select(ResumeAnalysisEntity)
        .where(ResumeAnalysisEntity.resume_id == resume_id)
        .order_by(ResumeAnalysisEntity.analyzed_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

  async def find_by_resume_id(
      self, resume_id: int
  ) -> list[ResumeAnalysisEntity]:
    result = await self.session.execute(
        select(ResumeAnalysisEntity)
        .where(ResumeAnalysisEntity.resume_id == resume_id)
        .order_by(ResumeAnalysisEntity.analyzed_at.desc())
    )
    return list(result.scalars().all())

  async def save(self, entity: ResumeAnalysisEntity) -> ResumeAnalysisEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def delete_by_resume_id(self, resume_id: int) -> None:
    result = await self.session.execute(
        select(ResumeAnalysisEntity).where(
            ResumeAnalysisEntity.resume_id == resume_id
        )
    )
    analyses = result.scalars().all()
    for analysis in analyses:
      await self.session.delete(analysis)
    await self.session.flush()
