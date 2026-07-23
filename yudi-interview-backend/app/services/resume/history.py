import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.models.resume import ResumeAnalysisEntity, ResumeEntity
from app.models.resume_dto import (
    AnalysisHistoryDTO,
    InterviewHistoryItemDTO,
    ResumeDetailDTO,
    ResumeListItemDTO,
)
from app.models.common import AsyncTaskStatus
from app.repositories.resume_repository import ResumeAnalysisRepository


log = logging.getLogger(__name__)


def _normalize_strengths(raw_strengths: Any) -> list[dict[str, str]]:
  if not isinstance(raw_strengths, list):
    return []

  normalized: list[dict[str, str]] = []
  for item in raw_strengths:
    if isinstance(item, dict):
      normalized.append({
          "category": str(item.get("category") or "亮点"),
          "description": str(
              item.get("description")
              or item.get("content")
              or item.get("text")
              or ""
          ),
      })
    elif item is not None:
      normalized.append({
          "category": "亮点",
          "description": str(item),
      })
  return [item for item in normalized if item["description"].strip()]


def _normalize_suggestions(raw_suggestions: Any) -> list[dict[str, str]]:
  if not isinstance(raw_suggestions, list):
    return []

  normalized: list[dict[str, str]] = []
  for item in raw_suggestions:
    if isinstance(item, dict):
      normalized.append({
          "category": str(item.get("category") or "综合"),
          "priority": str(item.get("priority") or "中"),
          "issue": str(item.get("issue") or item.get("problem") or ""),
          "recommendation": str(
              item.get("recommendation")
              or item.get("suggestion")
              or item.get("advice")
              or ""
          ),
      })
    elif item is not None:
      normalized.append({
          "category": "综合",
          "priority": "中",
          "issue": str(item),
          "recommendation": "",
      })
  return [
      item
      for item in normalized
      if item["issue"].strip() or item["recommendation"].strip()
  ]


class ResumeHistoryService:
  def __init__(self, session):
    self.session = session
    self.analysis_repo = ResumeAnalysisRepository(session)

  async def list_resumes(
      self, page: int = 1, page_size: int = 20
  ) -> tuple[list[ResumeListItemDTO], int]:
    from sqlalchemy import func as sql_func

    offset = (page - 1) * page_size
    total_result = await self.session.execute(
        select(func.count(ResumeEntity.id))
    )
    total = total_result.scalar_one()

    result = await self.session.execute(
        select(ResumeEntity)
        .order_by(ResumeEntity.uploaded_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    entities = list(result.scalars().all())

    items = []
    for entity in entities:
      latest_analysis = await self.analysis_repo.find_latest_by_resume_id(entity.id)
      items.append(
          ResumeListItemDTO(
              id=entity.id,
              filename=entity.original_filename,
              file_size=entity.file_size,
              uploaded_at=entity.uploaded_at,
              access_count=entity.access_count or 0,
              latest_score=latest_analysis.overall_score if latest_analysis else None,
              last_analyzed_at=latest_analysis.analyzed_at if latest_analysis else None,
              interview_count=0,
              analyze_status=AsyncTaskStatus(entity.analyze_status),
              analyze_error=entity.analyze_error,
          )
      )
    return items, total

  async def get_resume_detail(self, resume_id: int) -> ResumeDetailDTO | None:
    entity = await self.session.get(ResumeEntity, resume_id)
    if entity is None:
      return None

    analyses = await self.analysis_repo.find_by_resume_id(resume_id)
    analysis_dtos = []
    for a in analyses:
      strengths = []
      suggestions = []
      if a.strengths_json:
        try:
          strengths = _normalize_strengths(json.loads(a.strengths_json))
        except Exception:
          pass
      if a.suggestions_json:
        try:
          suggestions = _normalize_suggestions(json.loads(a.suggestions_json))
        except Exception:
          pass

      analysis_dtos.append(
          AnalysisHistoryDTO(
              id=a.id,
              overall_score=a.overall_score,
              content_score=a.content_score,
              structure_score=a.structure_score,
              skill_match_score=a.skill_match_score,
              expression_score=a.expression_score,
              project_score=a.project_score,
              summary=a.summary,
              analyzed_at=a.analyzed_at,
              strengths=strengths,
              suggestions=suggestions,
          )
      )

    return ResumeDetailDTO(
        id=entity.id,
        filename=entity.original_filename,
        file_size=entity.file_size,
        content_type=entity.content_type,
        storage_url=entity.storage_url,
        uploaded_at=entity.uploaded_at,
        access_count=entity.access_count or 0,
        resume_text=entity.resume_text,
        analyze_status=AsyncTaskStatus(entity.analyze_status),
        analyze_error=entity.analyze_error,
        analyses=analysis_dtos,
        interviews=[],
    )
