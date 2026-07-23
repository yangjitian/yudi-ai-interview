import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.interview import InterviewAnswerEntity, InterviewSessionEntity
from app.models.interview_dto import InterviewDetailDTO, InterviewReportDTO, InterviewSessionDTO, SessionListItemDTO
from app.repositories.interview_repository import InterviewAnswerRepository, InterviewRepository


log = logging.getLogger(__name__)


class InterviewHistoryService:
  def __init__(self, session_repo: InterviewRepository, answer_repo: InterviewAnswerRepository):
    self.session_repo = session_repo
    self.answer_repo = answer_repo

  async def list_sessions(self, page: int = 1, page_size: int = 20) -> tuple[list[SessionListItemDTO], int]:
    started_at = time.perf_counter()
    log.info("[PERF] list_sessions 开始: page=%d pageSize=%d", page, page_size)
    db_started_at = time.perf_counter()
    entities, total = await self.session_repo.list_sessions(page, page_size)
    log.info(
        "[PERF] list_sessions 数据库查询: %.3fs rows=%d total=%d",
        time.perf_counter() - db_started_at,
        len(entities),
        total,
    )
    dto_started_at = time.perf_counter()
    items = [
        SessionListItemDTO(
            id=e.id,
            session_id=e.session_id,
            skill_id=e.skill_id or "",
            difficulty=e.difficulty or "mid",
            resume_id=e.resume_id,
            total_questions=e.total_questions or 0,
            overall_score=e.overall_score,
            status=e.status,
            evaluate_status=e.evaluate_status,
            evaluate_error=e.evaluate_error,
            created_at=e.created_at,
            completed_at=e.completed_at,
        )
        for e in entities
    ]
    log.info(
        "[PERF] list_sessions DTO 组装: %.3fs",
        time.perf_counter() - dto_started_at,
    )
    log.info("[PERF] list_sessions 总耗时: %.3fs", time.perf_counter() - started_at)
    return items, total

  async def get_detail(self, session_id: str) -> InterviewDetailDTO | None:
    from app.services.interview.session_service import InterviewSessionService
    from app.infrastructure.redis.session_cache import SessionCache

    svc = InterviewSessionService(
        session_repo=self.session_repo,
        answer_repo=self.answer_repo,
        session_cache=SessionCache(),
    )
    return await svc.get_detail(session_id)

  async def export_pdf(
      self, session_id: str
  ) -> tuple[bytes, str]:
    from app.infrastructure.pdf.export import PdfExportService
    detail = await self.get_detail(session_id)
    if detail is None:
      from app.core.errors import BusinessException, ErrorCode
      raise BusinessException(ErrorCode.INTERVIEW_SESSION_NOT_FOUND)

    pdf_svc = PdfExportService()
    qa_details = [
        {
            "question_index": a.get("question_index"),
            "category": a.get("category") or "综合",
            "question": a.get("question", ""),
            "user_answer": a.get("user_answer", ""),
            "score": a.get("score"),
            "feedback": a.get("feedback", ""),
            "reference_answer": a.get("reference_answer", ""),
        }
        for a in detail.answers
    ]
    report = detail.report
    pdf_bytes = pdf_svc.export_interview_report(
        skill_id=detail.skill_id,
        overall_score=detail.overall_score or 0,
        qa_details=qa_details,
        strengths=report.strengths if report else [],
        improvements=report.improvements if report else [],
        session_id=detail.session_id,
        total_questions=detail.total_questions,
        status=detail.status,
        created_at=detail.created_at,
        completed_at=detail.completed_at,
        overall_feedback=report.overall_feedback if report else (detail.overall_feedback or ""),
    )
    return pdf_bytes, f"interview_{session_id}_report.pdf"
