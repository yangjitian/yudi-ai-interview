import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import _async_session_factory, get_db
from app.config.settings import get_settings
from app.core.errors import BusinessException, ErrorCode
from app.core.result import ApiResponse
from app.models.common import AsyncTaskStatus
from app.models.interview_dto import (
    CreateInterviewRequest,
    InterviewDetailDTO,
    InterviewReportDTO,
    InterviewSessionDTO,
    SessionListItemDTO,
    SubmitAnswerRequest,
    SubmitAnswerResponse,
)
from app.repositories.interview_repository import InterviewAnswerRepository, InterviewRepository
from app.infrastructure.redis.session_cache import SessionCache
from app.services.interview.history import InterviewHistoryService
from app.services.interview.session_service import InterviewSessionService


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/interview", tags=["面试"])


def _session_service(db: AsyncSession) -> InterviewSessionService:
  return InterviewSessionService(
      session_repo=InterviewRepository(db),
      answer_repo=InterviewAnswerRepository(db),
      session_cache=SessionCache(),
  )


@router.get("/sessions", response_model=ApiResponse[list[SessionListItemDTO]])
async def list_sessions(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  history_svc = InterviewHistoryService(
      session_repo=InterviewRepository(db),
      answer_repo=InterviewAnswerRepository(db),
  )
  items, total = await history_svc.list_sessions(page, page_size)
  return ApiResponse.success(data=items)


@router.post("/sessions", response_model=ApiResponse[InterviewSessionDTO])
async def create_session(
    req: CreateInterviewRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  svc = _session_service(db)
  dto = await svc.create_session(
      skill_id=req.skill_id,
      difficulty=req.difficulty,
      question_count=req.question_count,
      resume_text=req.resume_text,
      resume_id=req.resume_id,
      llm_provider=req.llm_provider,
      force_create=req.force_create,
      custom_categories=req.custom_categories,
      jd_text=req.jd_text,
  )
  return ApiResponse.success(data=dto)


@router.get("/sessions/{session_id}", response_model=ApiResponse[InterviewSessionDTO])
async def get_session(
    session_id: str = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  svc = _session_service(db)
  dto = await svc.get_session(session_id)
  return ApiResponse.success(data=dto)


@router.get("/sessions/{session_id}/question", response_model=ApiResponse[dict])
async def get_current_question(
    session_id: str = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  svc = _session_service(db)
  dto = await svc.get_session(session_id)
  questions = dto.questions
  idx = dto.current_index
  if idx >= len(questions):
    return ApiResponse.success(data={"completed": True, "message": "所有问题已回答完毕"})
  return ApiResponse.success(data={"completed": False, "question": questions[idx]})


@router.post("/sessions/{session_id}/answers", response_model=ApiResponse[SubmitAnswerResponse])
async def submit_answer(
    req: SubmitAnswerRequest,
    session_id: str = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  svc = _session_service(db)
  result = await svc.submit_answer(
      session_id=session_id,
      question_index=req.question_index,
      answer=req.answer,
  )
  return ApiResponse.success(data=result)


@router.get("/sessions/{session_id}/report", response_model=ApiResponse[InterviewReportDTO])
async def get_report(
    session_id: str = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  svc = _session_service(db)
  report = await svc.generate_report(session_id)
  return ApiResponse.success(data=report)


@router.get("/unfinished/{resume_id}", response_model=ApiResponse[InterviewSessionDTO | None])
async def find_unfinished_session(
    resume_id: int = Path(description="简历ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  svc = _session_service(db)
  try:
    dto = await svc.get_session(str(resume_id))
    return ApiResponse.success(data=dto)
  except BusinessException:
    return ApiResponse.success(data=None)


@router.put("/sessions/{session_id}/answers", response_model=ApiResponse[None])
async def save_answer_draft(
    req: SubmitAnswerRequest,
    session_id: str = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  svc = _session_service(db)
  await svc.save_answer_draft(
      session_id=session_id,
      question_index=req.question_index,
      answer=req.answer,
  )
  return ApiResponse.success()


@router.post("/sessions/{session_id}/complete", response_model=ApiResponse[None])
async def complete_interview(
    session_id: str = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  svc = _session_service(db)
  await svc.complete_interview(session_id)
  return ApiResponse.success()


@router.post(
    "/sessions/{session_id}/re-evaluate",
    response_model=ApiResponse[None],
    summary="重新触发评估（仅重评失败/未评估题目）",
)
async def re_evaluate_session(
    session_id: str = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  """保留已成功评估题目，仅重评失败或未评估题目。"""
  repo = InterviewRepository(db)
  entity = await repo.find_by_session_id(session_id)

  if entity is None:
    raise BusinessException(ErrorCode.INTERVIEW_SESSION_NOT_FOUND)

  if entity.status not in {"COMPLETED", "EVALUATED"}:
    raise BusinessException(ErrorCode.INTERVIEW_NOT_COMPLETED)

  entity.evaluate_status = AsyncTaskStatus.PENDING.value
  entity.evaluate_error = None
  entity.overall_score = None
  entity.overall_feedback = None
  entity.strengths_json = None
  entity.improvements_json = None
  entity.reference_answers_json = None

  answers = await repo.find_answers_by_session_id(session_id)
  reset_count = 0
  for answer in answers:
    # Q-02 决策前没有可持久化的逐题失败状态，只能全量清空后重新评估。
    answer.score = None
    answer.feedback = None
    answer.reference_answer = None
    answer.key_points_json = None
    answer.eval_status = None
    reset_count += 1

  await db.commit()

  from app.infrastructure.redis.evaluate_producer import send_evaluate_task
  await send_evaluate_task(session_id)
  log.info(
      "增量重新评估任务已入队: session_id=%s reset_questions=%d",
      session_id,
      reset_count,
  )

  return ApiResponse.success()


@router.get("/sessions/{session_id}/details", response_model=ApiResponse[InterviewDetailDTO])
async def get_session_detail(
    session_id: str = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  svc = _session_service(db)
  detail = await svc.get_detail(session_id)
  return ApiResponse.success(data=detail)


@router.get("/sessions/{session_id}/evaluation/events")
async def evaluation_events(
    session_id: str = Path(description="会话ID"),
) -> StreamingResponse:
  async def event_generator():
    last_status: str | None = None
    last_heartbeat_at = 0.0
    heartbeat_seconds = get_settings().interview.evaluation_sse_heartbeat_seconds
    log.info("[SSE] evaluation_connected | session_id=%s", session_id)
    try:
      while True:
        terminal = False
        status_changed = False
        async with _async_session_factory() as event_db:
          repo = InterviewRepository(event_db)
          session_entity = await repo.find_by_session_id(session_id)

          if session_entity is None:
            status = AsyncTaskStatus.FAILED.value
            payload = {
                "type": "failed",
                "evaluate_status": AsyncTaskStatus.FAILED.value,
                "error": "面试记录不存在",
            }
            terminal = True
          else:
            status = session_entity.evaluate_status or ""
            if status == AsyncTaskStatus.COMPLETED.value:
              payload = {
                  "type": "completed",
                  "evaluate_status": status,
                  "overall_score": session_entity.overall_score,
              }
              terminal = True
            elif status == AsyncTaskStatus.FAILED.value:
              payload = {
                  "type": "failed",
                  "evaluate_status": status,
                  "error": session_entity.evaluate_error or "评估失败",
              }
              terminal = True
            else:
              payload = {"type": "status", "evaluate_status": status}

            if status != last_status:
              status_changed = True
              log.info(
                  "[SSE] evaluation_status | session_id=%s status=%s",
                  session_id, status,
              )
              last_status = status

        now = asyncio.get_running_loop().time()
        if status_changed or terminal:
          yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        elif now - last_heartbeat_at >= heartbeat_seconds:
          yield ": keep-alive\n\n"
          last_heartbeat_at = now
        if terminal:
          return

        await asyncio.sleep(2)
    except asyncio.CancelledError:
      log.info("[SSE] evaluation_disconnected | session_id=%s", session_id)
    except Exception:
      log.exception("[SSE] evaluation_stream_error | session_id=%s", session_id)

  return StreamingResponse(
      event_generator(),
      media_type="text/event-stream",
      headers={
          "Cache-Control": "no-cache",
          "X-Accel-Buffering": "no",
          "Connection": "keep-alive",
      },
  )


@router.get("/sessions/{session_id}/export")
async def export_interview_pdf(
    session_id: str = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
  history_svc = InterviewHistoryService(
      session_repo=InterviewRepository(db),
      answer_repo=InterviewAnswerRepository(db),
  )
  pdf_bytes, filename = await history_svc.export_pdf(session_id)
  return StreamingResponse(
      iter([pdf_bytes]),
      media_type="application/pdf",
      headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
  )


@router.delete("/sessions/{session_id}", response_model=ApiResponse[None])
async def delete_session(
    session_id: str = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  repo = InterviewRepository(db)
  deleted = await repo.delete(session_id)
  if not deleted:
    raise BusinessException(ErrorCode.INTERVIEW_SESSION_NOT_FOUND)

  cache = SessionCache()
  await cache.delete_session(session_id)
  return ApiResponse.success()
