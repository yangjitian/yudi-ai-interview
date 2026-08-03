import asyncio
import base64
import hashlib
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import _async_session_factory, get_db
from app.config.settings import get_settings
from app.core.result import ApiResponse
from app.infrastructure.redis.voice_evaluate_producer import VoiceEvaluateStreamProducer
from app.infrastructure.storage.file_storage import (
    download_file,
    file_exists,
    upload_file,
)
from app.models.voice_dto import (
    CreateVoiceSessionRequest,
    VoiceEvaluationStatusDTO,
    VoiceInterviewMessageDTO,
    VoiceSessionMetaDTO,
    VoiceSessionResponseDTO,
    WebSocketControlMessage,
)
from app.repositories.voice_evaluation_repository import VoiceEvaluationRepository
from app.repositories.voice_message_repository import VoiceMessageRepository
from app.repositories.voice_session_repository import VoiceSessionRepository
from app.services.interview.voice_openings import (
    ALGORITHM_OPENING,
    BACKEND_OPENING,
    SKILL_OPENING_QUESTIONS,
)
from app.services.voice.asr_service import AsrService, AsrSession
from app.services.voice.llm_service import LlmServiceDirect
from app.services.voice.ordered_tts_emitter import OrderedTtsEmitter
from app.services.voice.session_service import VoiceInterviewSessionService
from app.services.voice.tts_service import TtsService

log = logging.getLogger(__name__)

# 模块级单例：TTS 连接池在主事件循环中创建并复用
_tts_service = TtsService()
_asr_service = AsrService()
_llm_service = LlmServiceDirect()
_opening_audio_cache: dict[str, str] = {}
_opening_audio_locks: dict[str, asyncio.Lock] = {}
_opening_audio_locks_guard = asyncio.Lock()
_tts_prewarm_tasks: set[asyncio.Task] = set()
_OPENING_WARMUP_SESSION_ID = "opening-audio-warmup"
_OPENING_SKILL_BY_TEXT = {
    text: skill_id for skill_id, text in SKILL_OPENING_QUESTIONS.items()
}
_OPENING_SKILL_BY_TEXT.setdefault(ALGORITHM_OPENING, "algorithm-default")
_OPENING_SKILL_BY_TEXT.setdefault(BACKEND_OPENING, "backend-default")

router = APIRouter(tags=["语音面试"])


def _voice_evaluate_producer() -> VoiceEvaluateStreamProducer:
  return VoiceEvaluateStreamProducer()


def _session_service(
    db: AsyncSession,
    producer: VoiceEvaluateStreamProducer | None = None,
) -> VoiceInterviewSessionService:
  return VoiceInterviewSessionService(
      session_repo=VoiceSessionRepository(db),
      message_repo=VoiceMessageRepository(db),
      evaluation_repo=VoiceEvaluationRepository(db),
      evaluate_producer=producer,
  )


# ==================== Endpoint 1: Create Session ====================


@router.post("/sessions", response_model=ApiResponse[VoiceSessionResponseDTO])
async def create_session(
    req: CreateVoiceSessionRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _session_service(db).create_session(req)
  task = asyncio.create_task(_prewarm_session_tts_pool(str(result.session_id)))
  _tts_prewarm_tasks.add(task)
  task.add_done_callback(_tts_prewarm_tasks.discard)
  return ApiResponse.success(data=result)


async def _prewarm_session_tts_pool(session_id: str) -> None:
  """创建会话后只预热连接，不合成任何开场白音频。"""
  started_at = time.perf_counter()
  try:
    await _tts_service.warmup_session(session_id)
    log.info(
        "[PERF] voice TTS pool prewarm: sessionId=%s elapsed=%.3fs",
        session_id,
        time.perf_counter() - started_at,
    )
  except Exception:
    log.exception("语音面试 TTS 连接预热失败: sessionId=%s", session_id)


# ==================== Endpoint 2: Get Session ====================


@router.get(
    "/sessions/{session_id}",
    response_model=ApiResponse[VoiceSessionResponseDTO],
)
async def get_session(
    session_id: int = Path(description="会话 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _session_service(db).get_session_dto(session_id)
  return ApiResponse.success(data=result)


# ==================== Endpoint 3: End Session ====================


@router.post("/sessions/{session_id}/end", response_model=ApiResponse[None])
async def end_session(
    session_id: int = Path(description="会话 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  await _session_service(db, _voice_evaluate_producer()).end_session(session_id)
  return ApiResponse.success()


# ==================== Endpoint 4: Pause Session ====================


@router.put("/sessions/{session_id}/pause", response_model=ApiResponse[None])
async def pause_session(
    session_id: int = Path(description="会话 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  await _session_service(db).pause_session(session_id)
  return ApiResponse.success()


# ==================== Endpoint 5: Resume Session ====================


@router.put(
    "/sessions/{session_id}/resume",
    response_model=ApiResponse[VoiceSessionResponseDTO],
)
async def resume_session(
    session_id: int = Path(description="会话 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _session_service(db).resume_session(session_id)
  return ApiResponse.success(data=result)


# ==================== Endpoint 6: List Sessions ====================


@router.get("/sessions", response_model=ApiResponse[list[VoiceSessionMetaDTO]])
async def list_sessions(
    user_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _session_service(db).get_all_sessions(user_id, status)
  return ApiResponse.success(data=result)


# ==================== Endpoint 7: Delete Session ====================


@router.delete("/sessions/{session_id}", response_model=ApiResponse[None])
async def delete_session(
    session_id: int = Path(description="会话 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  await _session_service(db).delete_session(session_id)
  return ApiResponse.success()


# ==================== Endpoint 8: Get Messages ====================


@router.get(
    "/sessions/{session_id}/messages",
    response_model=ApiResponse[list[VoiceInterviewMessageDTO]],
)
async def get_messages(
    session_id: int = Path(description="会话 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _session_service(db).get_messages(session_id)
  return ApiResponse.success(data=result)


# ==================== Endpoint 9/10: Evaluation ====================


@router.get(
    "/sessions/{session_id}/evaluation",
    response_model=ApiResponse[VoiceEvaluationStatusDTO],
)
async def get_evaluation(
    session_id: int = Path(description="会话 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _session_service(db).get_evaluation_status(session_id)
  return ApiResponse.success(data=result)


@router.post(
    "/sessions/{session_id}/evaluation",
    response_model=ApiResponse[VoiceEvaluationStatusDTO],
)
async def generate_evaluation(
    session_id: int = Path(description="会话 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _session_service(
      db, _voice_evaluate_producer()
  ).trigger_evaluation(session_id)
  return ApiResponse.success(data=result)


# ==================== WebSocket Router ====================

ws_router = APIRouter()


async def _synthesize_opening_audio(session_id: str, text: str) -> str:
  """按进程内缓存、RustFS、真实 TTS 的顺序获取开场白音频。"""
  cached = _opening_audio_cache.get(text)
  if cached:
    return cached

  async with _opening_audio_locks_guard:
    lock = _opening_audio_locks.setdefault(text, asyncio.Lock())

  async with lock:
    cached = _opening_audio_cache.get(text)
    if cached:
      return cached
    storage_key = _opening_audio_storage_key(text)
    try:
      if await file_exists(storage_key):
        wav_audio, _ = await download_file(storage_key)
        if wav_audio:
          audio_b64 = base64.b64encode(wav_audio).decode("ascii")
          _opening_audio_cache[text] = audio_b64
          log.info("Opening audio RustFS cache hit: key=%s", storage_key)
          return audio_b64
      log.info("Opening audio RustFS cache miss: key=%s", storage_key)
    except Exception as exc:
      log.warning(
          "Opening audio RustFS cache read failed: key=%s error=%s",
          storage_key,
          exc,
      )

    audio_b64 = await _synthesize_opening_audio_uncached(session_id, text)
    if audio_b64:
      _opening_audio_cache[text] = audio_b64
      try:
        await upload_file(
            base64.b64decode(audio_b64, validate=True),
            storage_key,
            "audio/wav",
        )
        log.info("Opening audio stored in RustFS: key=%s", storage_key)
      except Exception as exc:
        log.warning(
            "Opening audio RustFS cache write failed: key=%s error=%s",
            storage_key,
            exc,
        )
    return audio_b64


def _opening_audio_storage_key(text: str) -> str:
  skill_id = _OPENING_SKILL_BY_TEXT.get(text, "default")
  text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
  return f"voice-interview/openings/{skill_id}/{text_hash}.wav"


async def _synthesize_opening_audio_uncached(session_id: str, text: str) -> str:
  """合成单条开场白语音并返回 base64 WAV。"""
  chunks: list[bytes] = []
  done = asyncio.Event()
  errors: list[Exception] = []

  def on_audio(chunk: bytes) -> None:
    chunks.append(chunk)

  def on_complete() -> None:
    done.set()

  def on_error(e: Exception) -> None:
    errors.append(e)
    done.set()

  cfg = get_settings().voice_interview
  try:
    await _tts_service.synthesize_stream(
        session_id, text, on_audio, on_complete, on_error
    )
    await asyncio.wait_for(done.wait(), timeout=cfg.tts_timeout_seconds or 30)
  except Exception:
    log.exception("开场白 TTS 合成失败: sessionId=%s", session_id)
    return ""
  if errors:
    log.error("开场白 TTS 合成错误: sessionId=%s, error=%s", session_id, errors[0])
    return ""
  if not chunks:
    return ""
  return _tts_service.pcm_to_wav_base64(
      b"".join(chunks), cfg.tts_sample_rate or 24000
  )


async def warmup_opening_audio_cache() -> None:
  """后台预合成全部固定开场白，对齐 Java 启动预热行为。"""
  opening_texts = dict.fromkeys((
      *SKILL_OPENING_QUESTIONS.values(),
      ALGORITHM_OPENING,
      BACKEND_OPENING,
  ))
  for opening_text in opening_texts:
    await _synthesize_opening_audio(_OPENING_WARMUP_SESSION_ID, opening_text)
  log.info("开场白音频缓存预热完成: count=%s", len(_opening_audio_cache))


async def close_opening_audio_warmup_pool() -> None:
  await _tts_service.close_pool(_OPENING_WARMUP_SESSION_ID)


async def _start_asr_session(
    websocket: WebSocket,
    session_id: int,
    merge_state: dict[str, object],
) -> AsrSession:
  """启动 ASR 并将其状态、字幕回调转发到当前 WebSocket。"""
  async def on_partial(text: str) -> None:
    if merge_state["processing"]:
      return
    await websocket.send_json({
        "type": "subtitle",
        "text": _join_asr_segments(str(merge_state["confirmed"]), text),
        "isFinal": False,
    })

  async def on_sentence_end(text: str) -> None:
    if merge_state["processing"]:
      return
    merged = _join_asr_segments(str(merge_state["confirmed"]), text)
    merge_state["confirmed"] = merged
    await websocket.send_json({
        "type": "subtitle",
        "text": merged,
        "isFinal": False,
    })

  async def on_ready() -> None:
    await websocket.send_json({
        "type": "control",
        "action": "asr_ready",
        "message": "语音识别已就绪",
        "timestamp": int(time.time() * 1000),
    })

  async def on_error(error: Exception) -> None:
    log.error("语音识别失败: sessionId=%s, error=%s", session_id, error)
    await websocket.send_json({
        "type": "error",
        "message": f"语音识别失败: {error}",
    })

  return await _asr_service.create_session(
      on_partial=on_partial,
      on_sentence_end=on_sentence_end,
      on_ready=on_ready,
      on_error=on_error,
  )


def _join_asr_segments(previous: str, next_segment: str) -> str:
  previous = previous.strip()
  next_segment = next_segment.strip()
  if not previous:
    return next_segment
  if not next_segment:
    return previous
  if next_segment == previous or next_segment.startswith(previous):
    return next_segment
  if previous.endswith(next_segment):
    return previous
  separator = " " if previous[-1] in "。！？；.!?;" else "，"
  return previous + separator + next_segment


async def _forward_user_audio(asr_session: AsrSession, payload: dict) -> None:
  audio_b64 = payload.get("data")
  if not isinstance(audio_b64, str) or not audio_b64:
    raise ValueError("音频消息缺少 data")
  try:
    audio_data = base64.b64decode(audio_b64, validate=True)
  except (ValueError, TypeError) as exc:
    raise ValueError("音频 data 不是有效的 base64") from exc
  await asr_session.send_audio(audio_data)


async def _send_opening_question(websocket: WebSocket, session_id: int) -> None:
  """连接建立后自动推送开场白（对应 Java triggerOpeningQuestionIfNeeded）。

  文本先行下发，TTS 语音随后；准备阶段失败时记录日志并告知前端，不中断连接。
  """
  total_started_at = time.perf_counter()
  prepare_started_at = time.perf_counter()
  try:
    async with _async_session_factory() as db:
      opening_text = await _session_service(db).prepare_connection(session_id)
  except Exception:
    log.exception("准备开场白失败: sessionId=%s", session_id)
    await websocket.send_json({"type": "error", "message": "开场白初始化失败"})
    await _send_opening_complete(websocket)
    return

  log.info(
      "[PERF] voice opening prepare: sessionId=%s elapsed=%.3fs",
      session_id,
      time.perf_counter() - prepare_started_at,
  )

  if not opening_text:
    # 已有历史对话（重连/恢复）或会话不存在，不重复开场
    await _send_opening_complete(websocket)
    return

  synth_started_at = time.perf_counter()
  audio_b64 = await _synthesize_opening_audio(str(session_id), opening_text)
  log.info(
      "[PERF] voice opening TTS: sessionId=%s elapsed=%.3fs",
      session_id,
      time.perf_counter() - synth_started_at,
  )
  await websocket.send_json(
      {"type": "text", "content": opening_text, "final": True}
  )
  if audio_b64:
    await websocket.send_json(
        {"type": "audio", "data": audio_b64, "text": opening_text}
    )
    log.info(
        "[VOICE_TIMELINE] ts=%s session=%s event=opening_audio_sent",
        int(time.time() * 1000),
        session_id,
    )
  await _send_opening_complete(websocket)
  log.info(
      "Opening question sent for session %s (total=%.3fs)",
      session_id,
      time.perf_counter() - total_started_at,
  )


async def _send_opening_complete(websocket: WebSocket) -> None:
  """无开场白音频可播放时通知前端解除开场锁定。"""
  await websocket.send_json({
      "type": "control",
      "action": "opening_complete",
      "message": "开场白准备完成",
      "timestamp": int(time.time() * 1000),
  })


async def _handle_submit(
    websocket: WebSocket,
    session_id: int,
    control: WebSocketControlMessage,
) -> None:
  """处理前端提交的用户回答，触发 LLM 生成下一句追问
  （对应 Java handleControl "submit" → flushMergedUtteranceToLlm → triggerLlmResponse）。
  """
  data = control.data or {}
  raw_text = data.get("text")
  user_text = raw_text.strip() if isinstance(raw_text, str) else ""
  if not user_text:
    await websocket.send_json({
        "type": "control",
        "action": "submit_empty",
        "message": "未识别到有效回答",
        "timestamp": int(time.time() * 1000),
    })
    return
  await websocket.send_json({
      "type": "control",
      "action": "submit_accepted",
      "message": "回答已提交",
      "timestamp": int(time.time() * 1000),
  })

  async with _async_session_factory() as db:
    service = _session_service(db)
    entity = await service.get_session(session_id)
    if entity is None:
      log.error("Session entity not found for session %s, cannot generate LLM response", session_id)
      await websocket.send_json({"type": "error", "message": "会话不存在，请重新开始面试"})
      return
    history = await service.get_conversation_history(session_id)

  log.info(
      "Merged user utterance for session %s, triggering LLM (length %d)",
      session_id, len(user_text),
  )

  async def on_token(partial: str) -> None:
    if partial:
      await websocket.send_json({"type": "text", "content": partial, "final": False})

  cfg = get_settings().voice_interview

  async def send_audio_chunk(pcm_audio: bytes, index: int, is_last: bool) -> None:
    audio_b64 = _tts_service.pcm_to_wav_base64(
        pcm_audio, cfg.tts_sample_rate or 24000
    )
    await websocket.send_json({
        "type": "audio_chunk",
        "data": audio_b64,
        "index": index,
        "isLast": is_last,
    })
    log.info(
        "Audio chunk sent for session %s: index=%d, isLast=%s",
        session_id,
        index,
        is_last,
    )

  emitter = OrderedTtsEmitter(
      tts_service=_tts_service,
      session_id=str(session_id),
      send_fn_async=send_audio_chunk,
      max_concurrent=cfg.max_concurrent_tts_per_session or 3,
      timeout=cfg.tts_timeout_seconds or 30,
  )
  sentence_submitted = False

  def on_sentence(sentence: str) -> None:
    nonlocal sentence_submitted
    if sentence and sentence.strip():
      sentence_submitted = True
      emitter.submit(sentence)

  ai_reply = await _llm_service.chat_stream_sentences(
      user_text,
      on_token=on_token,
      on_sentence=on_sentence,
      session_entity=entity,
      conversation_history=history,
  )
  log.info("LLM response for session %s: '%s'", session_id, ai_reply)

  # 对齐 Java triggerLlmResponse 的下发顺序：final 字幕 → final 文本 → 落库 → 语音
  await websocket.send_json({"type": "subtitle", "text": user_text, "isFinal": True})
  await websocket.send_json({"type": "text", "content": ai_reply, "final": True})
  async with _async_session_factory() as db:
    await _session_service(db).save_message(session_id, user_text, ai_reply)
  if not sentence_submitted and ai_reply:
    emitter.submit(ai_reply)
  await emitter.drain()
  log.info("AI reply sent for session %s (length %d)", session_id, len(ai_reply))


@ws_router.websocket("/api/voice/ws/{session_id}")
@ws_router.websocket("/ws/voice-interview/{session_id}")
async def voice_interview_websocket(
    websocket: WebSocket,
    session_id: int,
) -> None:
  await websocket.accept()
  log.info(
      "[VOICE_TIMELINE] ts=%s session=%s event=websocket_accepted",
      int(time.time() * 1000),
      session_id,
  )
  await websocket.send_json({
      "type": "control",
      "action": "opening_started",
      "message": "面试官正在准备开场白",
      "timestamp": int(time.time() * 1000),
  })
  log.info(
      "[VOICE_TIMELINE] ts=%s session=%s event=opening_started_sent",
      int(time.time() * 1000),
      session_id,
  )
  asr_session: AsrSession | None = None
  merge_state: dict[str, object] = {"confirmed": "", "processing": False}
  forwarded_audio_logged = False
  # 对应 Java afterConnectionEstablished 的连接建立日志，保证 accept 后链路可追踪
  log.info("WebSocket connection established for session: %s", session_id)
  try:
    try:
      asr_session = await _start_asr_session(websocket, session_id, merge_state)
    except Exception:
      log.exception("初始化语音识别失败: sessionId=%s", session_id)
      await websocket.send_json({
          "type": "error",
          "message": "初始化语音识别失败，请检查语音服务配置",
      })
      return

    # 欢迎消息（对应 Java createWelcomeMessage）
    await websocket.send_json({
        "type": "control",
        "action": "welcome",
        "message": "连接成功，准备开始语音面试",
        "timestamp": int(time.time() * 1000),
    })
    # 自动开场白：文本 + 语音（对应 Java triggerOpeningQuestionIfNeeded）
    await _send_opening_question(websocket, session_id)
    while True:
      payload = await websocket.receive_json()
      if payload.get("type") == "audio":
        try:
          await _forward_user_audio(asr_session, payload)
          if not forwarded_audio_logged:
            log.info(
                "[VOICE_TIMELINE] ts=%s session=%s event=asr_audio_forwarded",
                int(time.time() * 1000),
                session_id,
            )
            forwarded_audio_logged = True
        except Exception as exc:
          log.error("处理用户音频失败: sessionId=%s, error=%s", session_id, exc)
          await websocket.send_json({
              "type": "error",
              "message": f"语音处理失败: {exc}",
          })
        continue
      if payload.get("type") != "control":
        continue
      control = WebSocketControlMessage.model_validate(payload)
      if control.action == "submit":
        # 对应 Java handleTextMessage 里 control 分支的 try/catch：处理失败告知前端，不断开连接
        try:
          merge_state["processing"] = True
          merge_state["confirmed"] = ""
          await _handle_submit(websocket, session_id, control)
        except WebSocketDisconnect:
          raise
        except Exception as exc:
          log.exception("处理用户回答失败: sessionId=%s", session_id)
          await websocket.send_json({
              "type": "error",
              "message": f"AI响应失败: {exc}",
          })
        finally:
          merge_state["processing"] = False
        continue
      async with _async_session_factory() as db:
        service = _session_service(db, _voice_evaluate_producer())
        if control.action == "start_phase":
          await service.start_phase(session_id, control.phase)
        elif control.action == "end_interview":
          await service.end_session(session_id)
          break
  except WebSocketDisconnect:
    pass
  finally:
    if asr_session is not None:
      await _asr_service.remove_session(asr_session.session_id)
    async with _async_session_factory() as db:
      await _session_service(
          db, _voice_evaluate_producer()
      ).end_session_if_in_progress(session_id)
