import asyncio
import json
import logging
from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import get_settings
from app.core.errors import BusinessException, ErrorCode
from app.core.result import ApiResponse
from app.models.common import AsyncTaskStatus
from app.models.voice_dto import (
    CreateVoiceSessionRequest,
    VoiceInterviewMessageDTO,
    VoiceSessionMetaDTO,
    VoiceSessionResponseDTO,
)
from app.models.voice_interview import (
    VoiceInterviewEvaluationEntity,
    VoiceInterviewMessageEntity,
    VoiceInterviewSessionEntity
)
from app.infrastructure.redis.voice_evaluate_producer import send_voice_evaluate_task
from app.services.voice.ws_handler import VoiceWebSocketHandler
from app.utils.timezone_utils import (
    get_beijing_now_naive,
    to_beijing_naive,
)


log = logging.getLogger(__name__)
router = APIRouter(tags=["语音面试"])
settings = get_settings()
DEFAULT_USER_ID = "default"
ws_router = APIRouter(tags=["voice-interview"])


def _phase_for_request(req: CreateVoiceSessionRequest) -> str:
    if req.intro_enabled:
        return "INTRO"
    if req.tech_enabled:
        return "TECH"
    if req.project_enabled:
        return "PROJECT"
    if req.hr_enabled:
        return "HR"
    return "COMPLETED"


def _websocket_url(request: Request | None, session_id: int) -> str:
    return f"/api/voice/ws/{session_id}"


def _session_response(entity: VoiceInterviewSessionEntity, request: Request | None = None) -> dict:
    return {
        "session_id": entity.id,
        "status": entity.status or "IN_PROGRESS",
        "current_phase": entity.current_phase,
        "planned_duration": entity.planned_duration,
        "web_socket_url": _websocket_url(request, entity.id),
    }


def _get_attr(entity, *names, default=None):
    for name in names:
        value = getattr(entity, name, None)
        if value is not None:
            return value
    return default


@router.post("/sessions", response_model=ApiResponse[VoiceSessionResponseDTO])
async def create_session(
    req: CreateVoiceSessionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    now = get_beijing_now_naive()
    effective_skill_id = req.skill_id or "java-backend"
    effective_role_type = req.role_type or effective_skill_id
    effective_planned_duration = req.planned_duration or 30
    if effective_planned_duration <= 0:
        effective_planned_duration = 30

    entity = VoiceInterviewSessionEntity(
        user_id=DEFAULT_USER_ID,
        role_type=effective_role_type,
        skill_id=effective_skill_id,
        difficulty=req.difficulty or "mid",
        status="IN_PROGRESS",
        current_phase=_phase_for_request(req),
        planned_duration=effective_planned_duration,
        intro_enabled=req.intro_enabled,
        tech_enabled=req.tech_enabled,
        project_enabled=req.project_enabled,
        hr_enabled=req.hr_enabled,
        resume_id=req.resume_id,
        llm_provider=req.llm_provider,
        custom_jd_text=req.custom_jd_text,
        start_time=now,
        created_at=now,
        updated_at=now,
        evaluate_status=None,
    )
    db.add(entity)
    await db.commit()
    await db.refresh(entity)

    # P0-1 优化：在 HTTP 会话创建阶段预热 TTS 连接池 + 预生成开场白
    # 目标：WS 握手时连接池已就绪，首帧延迟 < 1s
    try:
        from app.services.voice.tts_registry import (
            set_opening as _set_opening,
            warmup_pool_for_session as _warmup_pool,
        )
        from app.services.voice.tts_service import TtsService
        from app.services.voice.agent import VoiceInterviewAgent
        from app.config.settings import get_settings

        cfg = get_settings().voice_interview
        tts_svc = TtsService()

        # 1. 预生成开场白（同步模板，无 LLM 调用）
        agent = VoiceInterviewAgent(
            skill_id=effective_skill_id,
            difficulty=req.difficulty or "mid",
            planned_duration=effective_planned_duration,
        )
        try:
            opening_text = await agent.generate_greeting()
        except Exception as e:
            log.warning("[VoiceSession] generate_greeting failed: %s, using fallback", e)
            opening_text = "你好，欢迎参加面试。我们现在开始吧。"
        await _set_opening(str(entity.id), opening_text)

        # 2. 预热 TTS 连接池（后台 fire-and-forget；不影响 POST 响应）
        #    但这里需要 await 确保 HTTP 返回前已完成。
        #    设置 5s 超时作为兜底，超时也不阻塞 HTTP 返回
        try:
            await asyncio.wait_for(
                _warmup_pool(str(entity.id), tts_svc, cfg),
                timeout=5.0,
            )
            log.info("[VoiceSession] TTS pool pre-warmed for session=%s", entity.id)
        except asyncio.TimeoutError:
            log.warning("[VoiceSession] TTS prewarm timeout for session=%s, "
                        "WS will warmup on connect", entity.id)
        except Exception as e:
            log.warning("[VoiceSession] TTS prewarm failed for session=%s: %s", entity.id, e)
    except Exception as e:
        log.warning("[VoiceSession] P0-1 prewarm hook failed: %s", e)

    return ApiResponse.success(data=_session_response(entity, request))


@router.get("/sessions/{session_id}", response_model=ApiResponse[VoiceSessionResponseDTO])
async def get_session(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    entity = await db.get(VoiceInterviewSessionEntity, session_id)
    if entity is None:
        raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND, f"Session not found: {session_id}")
    return ApiResponse.success(data=_session_response(entity, request))


@router.get("/sessions/{session_id}/meta", response_model=ApiResponse[VoiceSessionMetaDTO])
async def get_session_meta(
    session_id: int,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    entity = await db.get(VoiceInterviewSessionEntity, session_id)
    if entity is None:
        raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND, f"会话不存在: {session_id}")

    return ApiResponse.success(data={
        "session_id": entity.id,
        "role_type": entity.role_type,
        "skill_id": entity.skill_id,
        "difficulty": entity.difficulty,
        "status": entity.status,
        "current_phase": entity.current_phase,
        "planned_duration": entity.planned_duration,
        "actual_duration": entity.actual_duration,
        "start_time": entity.start_time.isoformat() if entity.start_time else None,
        "end_time": entity.end_time.isoformat() if entity.end_time else None,
        "evaluate_status": entity.evaluate_status,
        "evaluate_error": entity.evaluate_error,
    })


@router.get("/sessions", response_model=ApiResponse[list[dict]])
async def list_sessions(
    user_id: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    query = select(VoiceInterviewSessionEntity)
    if user_id:
        query = query.where(VoiceInterviewSessionEntity.user_id == user_id)
    if status:
        query = query.where(VoiceInterviewSessionEntity.status == status.upper())
    query = query.order_by(VoiceInterviewSessionEntity.updated_at.desc().nullslast(), VoiceInterviewSessionEntity.created_at.desc())
    result = await db.execute(query)
    entities = result.scalars().all()

    items = []
    for entity in entities:
        msg_count_result = await db.execute(
            select(func.count(VoiceInterviewMessageEntity.id)).where(
                VoiceInterviewMessageEntity.session_id == entity.id
            )
        )
        evaluation = await _get_latest_evaluation(db, entity.id)
        items.append({
            "sessionId": entity.id,
            "sessionIdStr": str(entity.id),
            "roleType": entity.role_type,
            "skillId": entity.skill_id,
            "difficulty": entity.difficulty,
            "status": entity.status,
            "currentPhase": entity.current_phase or "",
            "createdAt": entity.created_at.isoformat() if entity.created_at else None,
            "updatedAt": entity.updated_at.isoformat() if entity.updated_at else (entity.created_at.isoformat() if entity.created_at else None),
            "actualDuration": entity.actual_duration,
            "messageCount": msg_count_result.scalar_one() or 0,
            "evaluateStatus": entity.evaluate_status,
            "evaluateError": entity.evaluate_error,
            "overallScore": evaluation.overall_score if evaluation else None,
        })
    return ApiResponse.success(data=items)


@router.post("/sessions/{session_id}/end", response_model=ApiResponse[dict])
async def end_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    entity = await db.get(VoiceInterviewSessionEntity, session_id)
    if entity is None:
        raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND, f"会话不存在: {session_id}")
    now = get_beijing_now_naive()
    entity.status = "COMPLETED"
    entity.current_phase = "COMPLETED"
    entity.end_time = now
    entity.updated_at = now
    if entity.start_time:
        entity.actual_duration = int(
            (now - to_beijing_naive(entity.start_time)).total_seconds()
        )
    entity.evaluate_status = AsyncTaskStatus.PENDING.value
    entity.evaluate_error = None
    await db.commit()
    # 关闭会话时同步关闭 TTS 池，释放资源
    try:
        from app.services.voice.tts_registry import close_pool_for_session
        await close_pool_for_session(str(session_id))
    except Exception as e:
        log.warning("Failed to close TTS pool for session %s: %s", session_id, e)
    await send_voice_evaluate_task(str(session_id))
    return ApiResponse.success(data={"session_id": session_id, "status": "COMPLETED"})


@router.put("/sessions/{session_id}/pause", response_model=ApiResponse[dict])
async def pause_session(
    session_id: int,
    request: dict | None = None,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    entity = await db.get(VoiceInterviewSessionEntity, session_id)
    if entity is None:
        raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND, f"会话不存在: {session_id}")
    if entity.status != "IN_PROGRESS":
        raise BusinessException(ErrorCode.BAD_REQUEST, f"会话状态为 {entity.status}，无法暂停")
    entity.status = "PAUSED"
    now = get_beijing_now_naive()
    entity.paused_at = now
    entity.updated_at = now
    await db.commit()
    reason = (request or {}).get("reason", "user_initiated")
    log.info("Session %s paused, reason=%s", session_id, reason)
    return ApiResponse.success(data={"session_id": session_id, "status": "PAUSED"})


@router.put("/sessions/{session_id}/resume", response_model=ApiResponse[dict])
async def resume_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    entity = await db.get(VoiceInterviewSessionEntity, session_id)
    if entity is None:
        raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND, f"会话不存在: {session_id}")
    if entity.status != "PAUSED":
        raise BusinessException(ErrorCode.BAD_REQUEST, f"会话状态为 {entity.status}，无法恢复")
    entity.status = "IN_PROGRESS"
    now = get_beijing_now_naive()
    entity.resumed_at = now
    entity.updated_at = now
    await db.commit()
    return ApiResponse.success(data={
        "session_id": session_id,
        "status": "IN_PROGRESS",
        "current_phase": entity.current_phase,
        "planned_duration": entity.planned_duration,
    })


@router.delete("/sessions/{session_id}", response_model=ApiResponse[dict])
async def delete_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    log.info("[DELETE] voice session %s", session_id)

    entity = await db.get(VoiceInterviewSessionEntity, session_id)
    if entity is None:
        raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND, f"会话不存在: {session_id}")

    # 1. 清理 Redis 缓存（如果存在）
    try:
        from app.infrastructure.redis.session_cache import get_session_cache_key
        from app.infrastructure.redis.client import get_redis_client

        redis = await get_redis_client()
        cache_key = get_session_cache_key(str(session_id))
        await redis.delete(cache_key)
        log.info("[DELETE] Redis cache key '%s' deleted", cache_key)
    except Exception as redis_err:
        log.warning("[DELETE] Redis cache cleanup failed (non-fatal): %s", redis_err)

    # 2. 先删子表（SQLAlchemy 2.0 需每步 flush 才真正执行）
    msgs = await db.execute(
        select(VoiceInterviewMessageEntity).where(VoiceInterviewMessageEntity.session_id == session_id)
    )
    for msg in msgs.scalars().all():
        await db.delete(msg)
    await db.flush()
    log.info("[DELETE] %d messages deleted", len(list(msgs.scalars().all())))

    evals = await db.execute(
        select(VoiceInterviewEvaluationEntity).where(VoiceInterviewEvaluationEntity.session_id == session_id)
    )
    eval_list = list(evals.scalars().all())
    for ev in eval_list:
        await db.delete(ev)
    await db.flush()
    log.info("[DELETE] %d evaluations deleted", len(eval_list))

    # 3. 删会话主记录
    await db.delete(entity)
    await db.flush()
    await db.commit()

    log.info("[DELETE] voice session %s fully deleted", session_id)
    return ApiResponse.success(data={"session_id": session_id, "deleted": True})


@router.get("/sessions/{session_id}/messages", response_model=ApiResponse[list[VoiceInterviewMessageDTO]])
async def get_session_messages(
    session_id: int,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    entity = await db.get(VoiceInterviewSessionEntity, session_id)
    if entity is None:
        raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND, f"会话不存在: {session_id}")
    msg_result = await db.execute(
        select(VoiceInterviewMessageEntity)
        .where(VoiceInterviewMessageEntity.session_id == entity.id)
        .order_by(VoiceInterviewMessageEntity.sequence_num, VoiceInterviewMessageEntity.created_at)
    )
    messages = [{
        "id": m.id,
        "session_id": entity.id,
        "message_type": m.message_type,
        "phase": m.phase,
        "user_recognized_text": m.user_recognized_text or "",
        "ai_generated_text": m.ai_generated_text or "",
        "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        "sequence_num": m.sequence_num,
    } for m in msg_result.scalars().all()]
    return ApiResponse.success(data=messages)


@router.get("/sessions/{session_id}/evaluation", response_model=ApiResponse[dict])
async def get_evaluation_detail(
    session_id: int,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    entity = await db.get(VoiceInterviewSessionEntity, session_id)
    if entity is None:
        raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND, f"会话不存在: {session_id}")

    evaluation = await _get_latest_evaluation(db, session_id)
    if evaluation is None:
        return ApiResponse.success(data={
            "session_id": entity.id,
            "status": entity.status,
            "evaluate_status": entity.evaluate_status,
            "evaluate_error": entity.evaluate_error,
            "overall_score": None,
            "overall_feedback": None,
            "strengths": [],
            "improvements": [],
            "reference_answers": [],
            "question_evaluations": [],
        })

    return ApiResponse.success(data=_evaluation_status_response(entity, evaluation))


@router.get("/sessions/{session_id}/evaluation/events")
async def evaluation_events(session_id: int) -> StreamingResponse:
    from app.config.database import _async_session_factory

    async def event_generator():
        last_status: str | None = None
        last_heartbeat_at = 0.0
        heartbeat_seconds = settings.interview.evaluation_sse_heartbeat_seconds
        log.info("[SSE] voice_evaluation_connected | session_id=%s", session_id)
        try:
            while True:
                async with _async_session_factory() as event_db:
                    entity = await event_db.get(VoiceInterviewSessionEntity, session_id)
                    if entity is None:
                        payload = {
                            "type": "failed",
                            "evaluate_status": AsyncTaskStatus.FAILED.value,
                            "error": "语音面试记录不存在",
                        }
                        terminal = True
                    else:
                        status = entity.evaluate_status or ""
                        terminal = status in {
                            AsyncTaskStatus.COMPLETED.value,
                            AsyncTaskStatus.COMPLETED_WITH_ERRORS.value,
                            AsyncTaskStatus.FAILED.value,
                        }
                        if status in {
                            AsyncTaskStatus.COMPLETED.value,
                            AsyncTaskStatus.COMPLETED_WITH_ERRORS.value,
                        }:
                            payload = {
                                "type": "completed",
                                "evaluate_status": status,
                            }
                        elif status == AsyncTaskStatus.FAILED.value:
                            payload = {
                                "type": "failed",
                                "evaluate_status": status,
                                "error": entity.evaluate_error or "评估失败",
                            }
                        else:
                            payload = {"type": "status", "evaluate_status": status}

                now = asyncio.get_running_loop().time()
                if entity is None or entity.evaluate_status != last_status or terminal:
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    last_status = entity.evaluate_status if entity else None
                elif now - last_heartbeat_at >= heartbeat_seconds:
                    yield ": keep-alive\n\n"
                    last_heartbeat_at = now
                if terminal:
                    return
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            log.info("[SSE] voice_evaluation_disconnected | session_id=%s", session_id)
        except Exception:
            log.exception("[SSE] voice_evaluation_stream_error | session_id=%s", session_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/sessions/{session_id}/evaluation", response_model=ApiResponse[dict])
async def trigger_evaluation(
    session_id: int,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    entity = await db.get(VoiceInterviewSessionEntity, session_id)
    if entity is None:
        raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND, f"会话不存在: {session_id}")
    if entity.evaluate_status in {AsyncTaskStatus.PENDING.value, AsyncTaskStatus.PROCESSING.value}:
        return ApiResponse.success(data={
            "session_id": entity.id,
            "status": entity.status,
            "evaluate_status": entity.evaluate_status,
            "evaluate_error": entity.evaluate_error,
            "overall_score": None,
            "overall_feedback": None,
            "strengths": [],
            "improvements": [],
            "reference_answers": [],
            "question_evaluations": [],
        })
    if entity.evaluate_status in {
        AsyncTaskStatus.COMPLETED.value,
        AsyncTaskStatus.COMPLETED_WITH_ERRORS.value,
    }:
        evaluation = await _get_latest_evaluation(db, session_id)
        return ApiResponse.success(data=_evaluation_status_response(entity, evaluation) if evaluation else {
            "session_id": entity.id,
            "status": entity.status,
            "evaluate_status": entity.evaluate_status,
            "evaluate_error": entity.evaluate_error,
            "overall_score": None,
            "overall_feedback": None,
            "strengths": [],
            "improvements": [],
            "reference_answers": [],
            "question_evaluations": [],
        })

    entity.evaluate_status = AsyncTaskStatus.PENDING.value
    entity.evaluate_error = None
    entity.updated_at = get_beijing_now_naive()
    await db.commit()
    await send_voice_evaluate_task(str(session_id))
    return ApiResponse.success(data={
        "session_id": entity.id,
        "status": entity.status,
        "evaluate_status": AsyncTaskStatus.PENDING.value,
        "evaluate_error": None,
        "overall_score": None,
        "overall_feedback": None,
        "strengths": [],
        "improvements": [],
        "reference_answers": [],
        "question_evaluations": [],
    })


@router.websocket("/ws/{session_id}")
async def voice_ws(websocket: WebSocket, session_id: int, db: AsyncSession = Depends(get_db)):
    handler = VoiceWebSocketHandler(websocket, session_id, db)
    await handler.handle()


@ws_router.websocket("/ws/voice-interview/{session_id}")
async def voice_ws_java_path(websocket: WebSocket, session_id: int, db: AsyncSession = Depends(get_db)):
    handler = VoiceWebSocketHandler(websocket, session_id, db)
    await handler.handle()


async def _get_latest_evaluation(db: AsyncSession, session_id: int) -> VoiceInterviewEvaluationEntity | None:
    result = await db.execute(
        select(VoiceInterviewEvaluationEntity)
        .where(VoiceInterviewEvaluationEntity.session_id == session_id)
        .order_by(VoiceInterviewEvaluationEntity.created_at.desc(), VoiceInterviewEvaluationEntity.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _loads_json_list(value: str | None):
    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        return []


def _evaluation_response(
    session: VoiceInterviewSessionEntity,
    evaluation: VoiceInterviewEvaluationEntity,
) -> dict:
    question_evaluations = _loads_json_list(evaluation.question_evaluations_json)
    answers = []
    for item in question_evaluations:
        if not isinstance(item, dict):
            continue
        answers.append({
            "questionIndex": item.get("question_index", item.get("questionIndex", 0)),
            "question": item.get("question", ""),
            "category": item.get("category", ""),
            "userAnswer": item.get("user_answer", item.get("userAnswer", "")),
            "score": item.get("score", 0),
            "feedback": item.get("feedback", ""),
            "referenceAnswer": item.get("reference_answer", item.get("referenceAnswer")),
            "keyPoints": item.get("key_points", item.get("keyPoints", [])),
        })
    return {
        "sessionId": session.id,
        "totalQuestions": len(answers),
        "overallScore": evaluation.overall_score,
        "overallFeedback": evaluation.overall_feedback,
        "strengths": _loads_json_list(evaluation.strengths_json),
        "improvements": _loads_json_list(evaluation.improvements_json),
        "answers": answers,
    }


def _evaluation_status_response(
    session: VoiceInterviewSessionEntity,
    evaluation: VoiceInterviewEvaluationEntity,
) -> dict:
    question_evaluations = _loads_json_list(evaluation.question_evaluations_json)
    return {
        "session_id": session.id,
        "status": session.status,
        "evaluate_status": session.evaluate_status,
        "evaluate_error": session.evaluate_error,
        "overall_score": evaluation.overall_score,
        "overall_feedback": evaluation.overall_feedback,
        "strengths": _loads_json_list(evaluation.strengths_json),
        "improvements": _loads_json_list(evaluation.improvements_json),
        "reference_answers": _loads_json_list(evaluation.reference_answers_json),
        "question_evaluations": question_evaluations,
    }
