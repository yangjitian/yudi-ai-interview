import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Path, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.constants import RESUME_MAX_UPLOAD_BYTES
from app.core.errors import BusinessException, ErrorCode
from app.core.result import ApiResponse
from app.core.upload_validation import (
    RESUME_EXTENSION_CONTENT_TYPES,
    check_upload_content_length,
    read_upload_with_limit,
    validate_upload_metadata,
)
from app.infrastructure.pdf.export import PdfExportService
from app.models.resume_dto import ResumeListItemDTO, ResumeDetailDTO, ResumeUploadResponseDTO
from app.repositories.interview_repository import InterviewRepository
from app.repositories.resume_repository import ResumeRepository, ResumeAnalysisRepository
from app.services.resume.delete import ResumeDeleteService
from app.services.resume.grading import ResumeGradingService
from app.services.resume.history import ResumeHistoryService
from app.services.resume.parse import ResumeParseService
from app.services.resume.persistence import ResumePersistenceService
from app.services.resume.upload import ResumeUploadService
from app.utils.file_hash import compute_sha256


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/resumes", tags=["简历管理"])


@router.post("/upload", response_model=ApiResponse[dict])
async def upload_and_analyze(
    request: Request,
    file: Annotated[UploadFile, File(description="简历文件")],
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  check_upload_content_length(
      request,
      RESUME_MAX_UPLOAD_BYTES,
      ErrorCode.FILE_TOO_LARGE,
  )
  filename = file.filename or "unknown"
  validate_upload_metadata(
      filename,
      file.content_type,
      RESUME_EXTENSION_CONTENT_TYPES,
      ErrorCode.RESUME_FILE_TYPE_NOT_SUPPORTED,
  )
  content = await read_upload_with_limit(
      file,
      RESUME_MAX_UPLOAD_BYTES,
      ErrorCode.FILE_TOO_LARGE,
  )
  if not content:
    raise BusinessException(ErrorCode.BAD_REQUEST, "文件内容为空")

  file_hash = compute_sha256(content)
  content_type = file.content_type

  parse_svc = ResumeParseService()
  upload_svc = ResumeUploadService(
      resume_repo=ResumeRepository(db),
      parse_service=parse_svc,
  )

  result = await upload_svc.upload_and_analyze(
      content=content,
      filename=filename,
      content_type=content_type,
      file_hash=file_hash,
  )

  if result.get("duplicate"):
    return ApiResponse.success(
        data=result,
        message="检测到相同简历，已返回历史分析结果",
    )
  return ApiResponse.success(data=result)


@router.get("", response_model=ApiResponse[list[ResumeListItemDTO]])
async def list_resumes(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  history_svc = ResumeHistoryService(db)
  items, total = await history_svc.list_resumes(page, page_size)
  return ApiResponse.success(data=items)


@router.get("/{resume_id}/detail", response_model=ApiResponse[ResumeDetailDTO])
async def get_resume_detail(
    resume_id: int = Path(description="简历ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  history_svc = ResumeHistoryService(db)
  detail = await history_svc.get_resume_detail(resume_id)
  if detail is None:
    raise BusinessException(ErrorCode.RESUME_NOT_FOUND, "简历不存在")
  return ApiResponse.success(data=detail)


@router.get("/{resume_id}/export")
async def export_resume_pdf(
    resume_id: int = Path(description="简历ID"),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
  history_svc = ResumeHistoryService(db)
  detail = await history_svc.get_resume_detail(resume_id)
  if detail is None:
    raise BusinessException(ErrorCode.RESUME_NOT_FOUND, "简历不存在")

  latest_analysis = None
  if detail.analyses:
    a = detail.analyses[0]
    latest_analysis = a

  pdf_svc = PdfExportService()

  if latest_analysis:
    pdf_bytes = pdf_svc.export_resume_analysis(
        filename=detail.filename,
        overall_score=latest_analysis.overall_score or 0,
        content_score=latest_analysis.content_score or 0,
        structure_score=latest_analysis.structure_score or 0,
        skill_match_score=latest_analysis.skill_match_score or 0,
        expression_score=latest_analysis.expression_score or 0,
        project_score=latest_analysis.project_score or 0,
        summary=latest_analysis.summary or "",
        strengths=latest_analysis.strengths,
        suggestions=[
            {"category": s.get("category", ""), "priority": s.get("priority", ""),
             "issue": s.get("issue", ""), "recommendation": s.get("recommendation", "")}
            if isinstance(s, dict) else {"category": "", "priority": "", "issue": str(s), "recommendation": ""}
            for s in latest_analysis.suggestions
        ],
    )
  else:
    pdf_bytes = pdf_svc.export_resume_analysis(
        filename=detail.filename,
        overall_score=0,
        content_score=0,
        structure_score=0,
        skill_match_score=0,
        expression_score=0,
        project_score=0,
        summary="暂无分析结果",
        strengths=[],
        suggestions=[],
    )

  return StreamingResponse(
      iter([pdf_bytes]),
      media_type="application/pdf",
      headers={
          "Content-Disposition": f"attachment; filename*=UTF-8''resume_{resume_id}_report.pdf"
      },
  )


@router.delete("/{resume_id}", response_model=ApiResponse[None])
async def delete_resume(
    resume_id: int = Path(description="简历ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  delete_svc = ResumeDeleteService(
      ResumeRepository(db),
      ResumeAnalysisRepository(db),
      InterviewRepository(db),
  )
  await delete_svc.delete_resume(resume_id)
  return ApiResponse.success()


@router.post("/{resume_id}/reanalyze", response_model=ApiResponse[None])
async def reanalyze_resume(
    resume_id: int = Path(description="简历ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  upload_svc = ResumeUploadService(
      resume_repo=ResumeRepository(db),
      parse_service=ResumeParseService(),
  )
  await upload_svc.reanalyze(resume_id)
  return ApiResponse.success()


@router.get("/health", response_model=ApiResponse[dict])
async def health() -> ApiResponse:
  return ApiResponse.success(data={"status": "UP", "service": "Resume Service"})


@router.get("/statistics", response_model=ApiResponse[dict])
async def get_statistics(db: AsyncSession = Depends(get_db)) -> ApiResponse:
  from sqlalchemy import select, func
  from app.models.resume import ResumeEntity
  from app.models.interview import InterviewSessionEntity

  total_count = await db.scalar(select(func.count(ResumeEntity.id)))
  total_access = await db.scalar(select(func.sum(ResumeEntity.access_count))) or 0
  total_interviews = await db.scalar(select(func.count(InterviewSessionEntity.id)))

  return ApiResponse.success(data={
      "totalCount": total_count or 0,
      "totalAccessCount": int(total_access),
      "totalInterviewCount": total_interviews or 0,
  })
