import logging
from datetime import timedelta

from app.core.errors import BusinessException, ErrorCode
from app.infrastructure.redis.voice_evaluate_producer import VoiceEvaluateStreamProducer
from app.models.voice_dto import (
    CreateVoiceSessionRequest,
    VoiceEvaluationStatusDTO,
    VoiceInterviewMessageDTO,
    VoiceSessionMetaDTO,
    VoiceSessionResponseDTO,
)
from app.models.voice_interview import (
    VoiceInterviewMessageEntity,
    VoiceInterviewSessionEntity,
)
from app.repositories.voice_evaluation_repository import VoiceEvaluationRepository
from app.repositories.voice_message_repository import VoiceMessageRepository
from app.repositories.voice_session_repository import VoiceSessionRepository
from app.services.interview.voice_openings import get_phase_opening
from app.services.voice.evaluation_service import VoiceInterviewEvaluationService
from app.utils.timezone_utils import get_beijing_now_naive

log = logging.getLogger(__name__)

# 会话状态常量（对应 Java VoiceInterviewSessionStatus 枚举）
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_PAUSED = "PAUSED"
STATUS_COMPLETED = "COMPLETED"

# 面试阶段常量（对应 Java InterviewPhase 枚举）
PHASE_INTRO = "INTRO"
PHASE_TECH = "TECH"
PHASE_PROJECT = "PROJECT"
PHASE_HR = "HR"
PHASE_COMPLETED = "COMPLETED"
PHASES = (
    PHASE_INTRO,
    PHASE_TECH,
    PHASE_PROJECT,
    PHASE_HR,
    PHASE_COMPLETED,
)

DEFAULT_USER_ID = "default"


class VoiceInterviewSessionService:
  """语音面试会话管理服务。

  状态机流转规则（照抄 Java VoiceInterviewService）：
  - create_session: status 不设置（由 WebSocket 连接建立时改为 IN_PROGRESS）
  - pause_session:  IN_PROGRESS → PAUSED（其他状态抛异常）
  - resume_session: PAUSED → IN_PROGRESS（其他状态抛异常）
  - end_session:    任意状态 → COMPLETED（不检查前置状态）
  """

  def __init__(
      self,
      session_repo: VoiceSessionRepository,
      message_repo: VoiceMessageRepository,
      evaluation_repo: VoiceEvaluationRepository,
      evaluate_producer: VoiceEvaluateStreamProducer | None = None,
  ):
    self.session_repo = session_repo
    self.message_repo = message_repo
    self.evaluation_repo = evaluation_repo
    self.evaluate_producer = evaluate_producer or VoiceEvaluateStreamProducer()

  async def create_session(
      self, req: CreateVoiceSessionRequest
  ) -> VoiceSessionResponseDTO:
    """创建语音面试会话（对应 Java VoiceInterviewService.createSession）。"""
    now = get_beijing_now_naive()
    effective_skill_id = req.skill_id or "java-backend"
    first_phase = self._determine_first_phase(req)

    entity = VoiceInterviewSessionEntity(
        user_id=DEFAULT_USER_ID,
        role_type=req.role_type or effective_skill_id,
        skill_id=effective_skill_id,
        difficulty=req.difficulty or "mid",
        custom_jd_text=req.custom_jd_text,
        resume_id=req.resume_id,
        intro_enabled=req.intro_enabled,
        tech_enabled=req.tech_enabled,
        project_enabled=req.project_enabled,
        hr_enabled=req.hr_enabled,
        llm_provider=req.llm_provider,
        planned_duration=req.planned_duration,
        current_phase=first_phase,
        created_at=now,
        updated_at=now,
    )
    saved = await self.session_repo.save(entity)
    log.info(
        "Created voice interview session: %s, skill: %s, phase: %s",
        saved.id, effective_skill_id, first_phase,
    )
    return self._to_response_dto(saved)

  async def get_session_dto(self, session_id: int) -> VoiceSessionResponseDTO:
    """获取会话 DTO（对应 Java VoiceInterviewService.getSessionDTO）。"""
    entity = await self._get_or_throw(session_id)
    return self._to_response_dto(entity)

  async def get_session(
      self, session_id: int
  ) -> VoiceInterviewSessionEntity | None:
    """获取会话实体（对应 Java VoiceInterviewService.getSession(Long)）。"""
    return await self.session_repo.find_by_id(session_id)

  async def prepare_connection(self, session_id: int) -> str | None:
    """WebSocket 连接建立时的初始化（对应 Java triggerOpeningQuestionIfNeeded）。

    - 已有历史消息（重连/恢复）时不重复开场，返回 None
    - 首次连接：状态置为 IN_PROGRESS 并记录开始时间，落库开场白消息
    返回开场白文本；None 表示无需开场。
    """
    entity = await self.session_repo.find_by_id(session_id)
    if entity is None:
      log.warning("Cannot prepare connection - session not found: %s", session_id)
      return None
    if await self.message_repo.count_by_session_id(session_id) > 0:
      # 重连/恢复场景不重复开场；显式记日志，避免该分支静默难以排查
      log.info("Session %s already has history, skip opening question", session_id)
      return None
    if entity.status != STATUS_IN_PROGRESS:
      now = get_beijing_now_naive()
      entity.status = STATUS_IN_PROGRESS
      if entity.start_time is None:
        # start_time 列是 timestamp without time zone，读回为 naive，写入需保持一致
        entity.start_time = get_beijing_now_naive()
      entity.updated_at = now
      await self.session_repo.save(entity)
      await self.session_repo.commit()
    opening_text = get_phase_opening(
        entity.skill_id or "", entity.current_phase or PHASE_INTRO
    )
    log.info(
        "Preparing voice opening: session=%s, skill=%s, phase=%s, text=%s",
        session_id,
        entity.skill_id,
        entity.current_phase,
        opening_text,
    )
    await self.save_message(session_id, None, opening_text)
    return opening_text

  async def start_phase(self, session_id: int, phase: str | None) -> bool:
    """按 WebSocket 控制消息手动切换相位，不触发结束或评估。"""
    entity = await self.session_repo.find_by_id(session_id)
    if entity is None:
      log.warning("Cannot start phase - session not found: %s", session_id)
      return False

    normalized_phase = (phase or "").upper()
    if normalized_phase not in PHASES:
      log.error("Invalid phase string: %s", phase)
      return False

    old_phase = entity.current_phase
    entity.current_phase = normalized_phase
    entity.updated_at = get_beijing_now_naive()
    await self.session_repo.save(entity)
    await self.session_repo.commit()
    log.info(
        "Session %s transitioned from phase %s to %s",
        session_id,
        old_phase,
        normalized_phase,
    )
    return True

  @staticmethod
  def get_next_phase(entity: VoiceInterviewSessionEntity) -> str:
    """返回当前相位之后第一个启用相位，仅供显式调用。"""
    if entity.current_phase is None:
      if entity.intro_enabled:
        return PHASE_INTRO
      if entity.tech_enabled:
        return PHASE_TECH
      if entity.project_enabled:
        return PHASE_PROJECT
      if entity.hr_enabled:
        return PHASE_HR
      return PHASE_COMPLETED
    if entity.current_phase == PHASE_INTRO:
      if entity.tech_enabled:
        return PHASE_TECH
      if entity.project_enabled:
        return PHASE_PROJECT
      if entity.hr_enabled:
        return PHASE_HR
    elif entity.current_phase == PHASE_TECH:
      if entity.project_enabled:
        return PHASE_PROJECT
      if entity.hr_enabled:
        return PHASE_HR
    elif entity.current_phase == PHASE_PROJECT and entity.hr_enabled:
      return PHASE_HR
    return PHASE_COMPLETED

  async def get_all_sessions(
      self, user_id: str | None = None, status: str | None = None
  ) -> list[VoiceSessionMetaDTO]:
    """获取用户所有会话（对应 Java VoiceInterviewService.getAllSessions）。"""
    effective_user_id = user_id or DEFAULT_USER_ID
    entities = await self.session_repo.find_by_user_id(
        effective_user_id, status=status
    )
    # 分数与详情页同源：批量从评估表取 overall_score，避免逐条查库
    score_map = await self.evaluation_repo.find_scores_by_session_ids(
        [entity.id for entity in entities]
    )
    result = []
    for entity in entities:
      msg_count = await self.message_repo.count_by_session_id(entity.id)
      result.append(
          self._to_meta_dto(entity, msg_count, score_map.get(entity.id))
      )
    return result

  async def end_session(self, session_id: int) -> None:
    """结束面试会话（对应 Java VoiceInterviewService.endSession）。

    状态机：任意状态 → COMPLETED，不检查前置状态。
    设置 evaluate_status=PENDING 为后续评估做准备。
    """
    entity = await self._get_or_throw(session_id)
    await self._complete_session(entity)
    await self._send_evaluate_task_after_commit(session_id)

  async def end_session_if_in_progress(self, session_id: int) -> bool:
    entity = await self.session_repo.find_by_id(session_id)
    if entity is None or entity.status != STATUS_IN_PROGRESS:
      return False
    await self._complete_session(entity)
    await self._send_evaluate_task_after_commit(session_id)
    return True

  async def _complete_session(self, entity: VoiceInterviewSessionEntity) -> None:
    now = get_beijing_now_naive()
    entity.status = STATUS_COMPLETED
    entity.current_phase = PHASE_COMPLETED
    entity.end_time = now
    entity.updated_at = now
    duration = self._calc_duration_seconds(entity)
    if duration is not None:
      entity.actual_duration = duration
    entity.evaluate_status = "PENDING"
    entity.evaluate_error = None
    await self.session_repo.save(entity)
    await self.session_repo.commit()
    log.info("Ended voice interview session: %s", entity.id)

  @staticmethod
  def _calc_duration_seconds(entity: VoiceInterviewSessionEntity) -> int | None:
    """计算实际时长（秒）；start_time 为 DB 读回的 naive 时间，需用 naive 当前时间相减。"""
    if entity.start_time is None:
      return None
    return int((get_beijing_now_naive() - entity.start_time).total_seconds())

  async def trigger_evaluation(self, session_id: int) -> VoiceEvaluationStatusDTO:
    entity = await self._get_or_throw(session_id)
    if entity.evaluate_status in ("COMPLETED", "PROCESSING"):
      return await self._to_evaluation_status(entity)

    entity.evaluate_status = "PENDING"
    entity.evaluate_error = None
    entity.updated_at = get_beijing_now_naive()
    await self.session_repo.save(entity)
    await self.session_repo.commit()
    await self._send_evaluate_task_after_commit(session_id)
    return await self._to_evaluation_status(entity)

  async def get_evaluation_status(self, session_id: int) -> VoiceEvaluationStatusDTO:
    return await self._to_evaluation_status(await self._get_or_throw(session_id))

  async def cleanup_stale_sessions(self) -> int:
    stale_sessions = await self.session_repo.find_stale_in_progress(
        get_beijing_now_naive() - timedelta(hours=2)
    )
    if not stale_sessions:
      return 0

    for entity in stale_sessions:
      now = get_beijing_now_naive()
      entity.status = STATUS_COMPLETED
      entity.current_phase = PHASE_COMPLETED
      entity.end_time = now
      entity.updated_at = now
      duration = self._calc_duration_seconds(entity)
      if duration is not None:
        entity.actual_duration = duration
      entity.evaluate_status = "PENDING"
      entity.evaluate_error = None
      await self.session_repo.save(entity)
    await self.session_repo.commit()

    for entity in stale_sessions:
      await self._send_evaluate_task_after_commit(entity.id)
    return len(stale_sessions)

  async def _send_evaluate_task_after_commit(self, session_id: int) -> None:
    try:
      sent = await self.evaluate_producer.send_evaluate_task(session_id)
    except Exception:
      log.exception("发送语音面试评估任务失败: sessionId=%s", session_id)
      sent = False
    if sent:
      return
    entity = await self.session_repo.find_by_id(session_id)
    if entity is None:
      return
    entity.evaluate_status = "FAILED"
    entity.evaluate_error = "任务入队失败"
    entity.updated_at = get_beijing_now_naive()
    await self.session_repo.save(entity)
    await self.session_repo.commit()

  async def _to_evaluation_status(
      self, entity: VoiceInterviewSessionEntity
  ) -> VoiceEvaluationStatusDTO:
    if entity.evaluate_status is None:
      raise BusinessException(ErrorCode.VOICE_EVALUATION_NOT_FOUND)

    evaluation_data = None
    if entity.evaluate_status == "COMPLETED":
      evaluation = await self.evaluation_repo.find_by_session_id(entity.id)
      if evaluation is None:
        raise BusinessException(ErrorCode.VOICE_EVALUATION_NOT_FOUND)
      evaluation_data = VoiceInterviewEvaluationService.build_detail(evaluation)
    return VoiceEvaluationStatusDTO(
        evaluate_status=entity.evaluate_status,
        evaluate_error=entity.evaluate_error,
        evaluate_status_updated_at=entity.updated_at,
        evaluation=evaluation_data,
    )

  async def pause_session(self, session_id: int, reason: str = "user_initiated") -> None:
    """暂停面试会话（对应 Java VoiceInterviewService.pauseSession）。

    状态机：仅 IN_PROGRESS → PAUSED，其他状态抛 BAD_REQUEST。
    """
    entity = await self._get_or_throw(session_id)
    if entity.status != STATUS_IN_PROGRESS:
      raise BusinessException(
          ErrorCode.BAD_REQUEST,
          f"会话状态为 {entity.status}，无法暂停",
      )
    entity.status = STATUS_PAUSED
    entity.paused_at = get_beijing_now_naive()
    entity.updated_at = get_beijing_now_naive()
    await self.session_repo.save(entity)
    log.info("Session %d paused, reason: %s", session_id, reason)

  async def resume_session(
      self, session_id: int
  ) -> VoiceSessionResponseDTO:
    """恢复面试会话（对应 Java VoiceInterviewService.resumeSession）。

    状态机：仅 PAUSED → IN_PROGRESS，其他状态抛 BAD_REQUEST。
    """
    entity = await self._get_or_throw(session_id)
    if entity.status != STATUS_PAUSED:
      raise BusinessException(
          ErrorCode.BAD_REQUEST,
          f"会话状态为 {entity.status}，无法恢复",
      )
    entity.status = STATUS_IN_PROGRESS
    entity.resumed_at = get_beijing_now_naive()
    entity.updated_at = get_beijing_now_naive()
    saved = await self.session_repo.save(entity)
    msg_count = await self.message_repo.count_by_session_id(session_id)
    log.info(
        "Session %d resumed with %d messages in conversation history",
        session_id, msg_count,
    )
    return self._to_response_dto(saved)

  async def delete_session(self, session_id: int) -> None:
    """删除语音面试会话（对应 Java VoiceInterviewService.deleteSession）。

    级联删除评估记录和消息记录。
    """
    if not await self.session_repo.exists_by_id(session_id):
      raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND)
    # 删除关联的评估记录
    evaluation = await self.evaluation_repo.find_by_session_id(session_id)
    if evaluation is not None:
      await self.evaluation_repo.delete(evaluation)
    # 删除关联的消息记录
    await self.message_repo.delete_by_session_id(session_id)
    # 删除会话
    await self.session_repo.delete(session_id)
    log.info("Deleted voice interview session: %d", session_id)

  async def get_messages(
      self, session_id: int
  ) -> list[VoiceInterviewMessageDTO]:
    """获取对话历史（对应 Java VoiceInterviewService.getConversationHistoryDTO）。"""
    await self._get_or_throw(session_id)
    messages = await self.message_repo.find_by_session_id(session_id)
    return [self._to_message_dto(msg) for msg in messages]

  async def get_conversation_history(self, session_id: int) -> list[str]:
    """拼装 LLM 对话历史（对应 Java VoiceInterviewWebSocketHandler.getHistory）。

    把消息配对为「面试官：/候选人：」文本行；AI 提问后等待回答时先挂起，
    下一条消息到来时再按顺序补入，保持与 Java 一致的配对顺序。
    """
    messages = await self.message_repo.find_by_session_id(session_id)
    history: list[str] = []
    pending_ai_question: str | None = None
    for msg in messages:
      ai_text = self._trim_to_none(msg.ai_generated_text)
      user_text = self._trim_to_none(msg.user_recognized_text)

      if pending_ai_question is not None:
        history.append(f"面试官：{pending_ai_question}")
        pending_ai_question = None
        if user_text is not None:
          history.append(f"候选人：{user_text}")
        if ai_text is not None:
          pending_ai_question = ai_text
        continue

      if ai_text is not None and user_text is not None:
        history.append(f"面试官：{ai_text}")
        history.append(f"候选人：{user_text}")
      elif ai_text is not None:
        pending_ai_question = ai_text
      elif user_text is not None:
        history.append(f"候选人：{user_text}")
    if pending_ai_question is not None:
      history.append(f"面试官：{pending_ai_question}")
    return history

  async def save_message(
      self,
      session_id: int,
      user_text: str | None,
      ai_text: str | None,
  ) -> None:
    """保存一轮对话，并把用户文本回填到最近的待回答 AI 消息。"""
    entity = await self.session_repo.find_by_id(session_id)
    if entity is None:
      log.warning("Cannot save message - session not found: %s", session_id)
      return

    normalized_user_text = self._trim_to_none(user_text)
    normalized_ai_text = self._trim_to_none(ai_text)
    answer_attached = False
    if normalized_user_text is not None:
      unanswered = await self.message_repo.find_latest_unanswered_question(session_id)
      if unanswered is not None:
        unanswered.user_recognized_text = normalized_user_text
        await self.message_repo.save(unanswered)
        answer_attached = True

    if normalized_ai_text is not None:
      now = get_beijing_now_naive()
      message = VoiceInterviewMessageEntity(
          session_id=session_id,
          message_type="DIALOGUE",
          phase=entity.current_phase,
          user_recognized_text=(
              normalized_user_text
              if normalized_user_text is not None and not answer_attached
              else None
          ),
          ai_generated_text=normalized_ai_text,
          timestamp=now,
          created_at=now,
          sequence_num=await self.message_repo.count_by_session_id(session_id) + 1,
      )
      await self.message_repo.save(message)

    if answer_attached or normalized_ai_text is not None:
      await self.session_repo.commit()

  @staticmethod
  def _trim_to_none(text: str | None) -> str | None:
    if text is None or not text.strip():
      return None
    return text.strip()

  # ==================== Private Helpers ====================

  async def _get_or_throw(
      self, session_id: int
  ) -> VoiceInterviewSessionEntity:
    entity = await self.session_repo.find_by_id(session_id)
    if entity is None:
      raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND)
    return entity

  @staticmethod
  def _determine_first_phase(req: CreateVoiceSessionRequest) -> str:
    """根据启用的阶段确定第一个阶段（对应 Java determineFirstPhase）。"""
    if req.intro_enabled:
      return PHASE_INTRO
    if req.tech_enabled:
      return PHASE_TECH
    if req.project_enabled:
      return PHASE_PROJECT
    if req.hr_enabled:
      return PHASE_HR
    return PHASE_COMPLETED

  @staticmethod
  def _to_response_dto(
      entity: VoiceInterviewSessionEntity,
  ) -> VoiceSessionResponseDTO:
    return VoiceSessionResponseDTO(
        session_id=entity.id,
        status=entity.status or "CREATED",
        current_phase=entity.current_phase,
        planned_duration=entity.planned_duration,
    )

  @staticmethod
  def _to_meta_dto(
      entity: VoiceInterviewSessionEntity,
      message_count: int,
      overall_score: int | None = None,
  ) -> VoiceSessionMetaDTO:
    return VoiceSessionMetaDTO(
        session_id=entity.id,
        role_type=entity.role_type,
        skill_id=entity.skill_id,
        difficulty=entity.difficulty,
        status=entity.status,
        current_phase=entity.current_phase,
        planned_duration=entity.planned_duration,
        actual_duration=entity.actual_duration,
        created_at=entity.created_at.isoformat() if entity.created_at else None,
        updated_at=entity.updated_at.isoformat() if entity.updated_at else None,
        start_time=entity.start_time.isoformat() if entity.start_time else None,
        end_time=entity.end_time.isoformat() if entity.end_time else None,
        evaluate_status=entity.evaluate_status,
        evaluate_error=entity.evaluate_error,
        overall_score=overall_score,
        message_count=message_count,
    )

  @staticmethod
  def _to_message_dto(
      msg: VoiceInterviewMessageEntity,
  ) -> VoiceInterviewMessageDTO:
    return VoiceInterviewMessageDTO(
        id=msg.id,
        session_id=msg.session_id,
        message_type=msg.message_type,
        phase=msg.phase,
        user_recognized_text=msg.user_recognized_text or "",
        ai_generated_text=msg.ai_generated_text or "",
        timestamp=msg.timestamp.isoformat() if msg.timestamp else None,
        sequence_num=msg.sequence_num,
    )
