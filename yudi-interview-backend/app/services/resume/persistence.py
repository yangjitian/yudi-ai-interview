import logging
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.resume import ResumeAnalysisEntity, ResumeEntity
from app.models.common import AsyncTaskStatus
from app.utils.timezone_utils import get_beijing_now_naive

log = logging.getLogger(__name__)


class ResumePersistenceService:
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
        select(func.count()).select_from(ResumeEntity).where(
            ResumeEntity.file_hash == file_hash
        )
    )
    return result.scalar_one() > 0

  async def save_resume(
      self,
      file_hash: str,
      original_filename: str,
      file_size: int | None,
      content_type: str | None,
      storage_key: str,
      storage_url: str,
      resume_text: str,
  ) -> ResumeEntity:
    entity = ResumeEntity(
        file_hash=file_hash,
        original_filename=original_filename,
        file_size=file_size,
        content_type=content_type,
        storage_key=storage_key,
        storage_url=storage_url,
        resume_text=resume_text,
        uploaded_at=get_beijing_now_naive(),
        last_accessed_at=get_beijing_now_naive(),
        access_count=1,
        analyze_status=AsyncTaskStatus.PENDING.value,
    )
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def update_status(
      self,
      resume_id: int,
      status: AsyncTaskStatus,
      error: str | None = None,
  ) -> None:
    entity = await self.find_by_id(resume_id)
    if entity:
      entity.analyze_status = status.value
      entity.analyze_error = error

  async def save_analysis(
      self,
      resume_id: int,
      overall_score: int,
      content_score: int,
      structure_score: int,
      skill_match_score: int,
      expression_score: int,
      project_score: int,
      summary: str,
      strengths: list[str] | list[dict[str, str]],
      suggestions: list[dict[str, str]],
  ) -> ResumeAnalysisEntity:
    import json

    normalized_strengths: list[dict[str, str]] = []
    for item in strengths:
      if isinstance(item, dict):
        description = str(
            item.get("description")
            or item.get("content")
            or item.get("text")
            or ""
        )
        if description.strip():
          normalized_strengths.append({
              "category": str(item.get("category") or "亮点"),
              "description": description,
          })
      elif item is not None:
        description = str(item)
        if description.strip():
          normalized_strengths.append({
              "category": "亮点",
              "description": description,
          })

    analysis = ResumeAnalysisEntity(
        resume_id=resume_id,
        overall_score=overall_score,
        content_score=content_score,
        structure_score=structure_score,
        skill_match_score=skill_match_score,
        expression_score=expression_score,
        project_score=project_score,
        summary=summary,
        strengths_json=json.dumps(normalized_strengths, ensure_ascii=False),
        suggestions_json=json.dumps(suggestions, ensure_ascii=False),
        analyzed_at=get_beijing_now_naive(),
    )
    self.session.add(analysis)
    await self.session.flush()
    await self.session.refresh(analysis)
    return analysis

  async def get_latest_analysis(
      self, resume_id: int
  ) -> ResumeAnalysisEntity | None:
    result = await self.session.execute(
        select(ResumeAnalysisEntity)
        .where(ResumeAnalysisEntity.resume_id == resume_id)
        .order_by(ResumeAnalysisEntity.analyzed_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

  async def get_analysis_history(
      self, resume_id: int
  ) -> list[ResumeAnalysisEntity]:
    result = await self.session.execute(
        select(ResumeAnalysisEntity)
        .where(ResumeAnalysisEntity.resume_id == resume_id)
        .order_by(ResumeAnalysisEntity.analyzed_at.desc())
    )
    return list(result.scalars().all())

  async def delete_resume(self, resume_id: int) -> bool:
    entity = await self.find_by_id(resume_id)
    if entity is None:
      return False
    await self.session.delete(entity)
    await self.session.flush()
    return True

  async def list_resumes(self, page: int = 1, page_size: int = 20) -> tuple[list[ResumeEntity], int]:
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
    return list(result.scalars().all()), total

  async def increment_access(self, resume_id: int) -> None:
    entity = await self.find_by_id(resume_id)
    if entity:
      entity.access_count = (entity.access_count or 0) + 1
      entity.last_accessed_at = get_beijing_now_naive()
