import asyncio
import json
import logging
import time
from app.utils.timezone_utils import get_beijing_now_naive, to_beijing_naive

from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.stream_constants import (
    BATCH_SIZE,
    FIELD_RETRY_COUNT,
    FIELD_VOICE_SESSION_ID,
    MAX_RETRY_COUNT,
    POLL_INTERVAL_MS,
    STREAM_MAX_LEN,
    VOICE_EVALUATE_CONSUMER_PREFIX,
    VOICE_EVALUATE_GROUP_NAME,
    VOICE_EVALUATE_STREAM_KEY,
)


log = logging.getLogger(__name__)


async def send_voice_evaluate_task(session_id: str) -> None:
  client = await get_redis()
  try:
    await client.xadd(
        VOICE_EVALUATE_STREAM_KEY,
        {
            FIELD_VOICE_SESSION_ID: session_id,
            FIELD_RETRY_COUNT: "0",
        },
        maxlen=STREAM_MAX_LEN,
        approximate=True,
    )
    log.info("Voice evaluate task sent: sessionId=%s", session_id)
  except Exception as e:
    log.error("Send voice evaluate task failed: sessionId=%s error=%s", session_id, e)
    raise


class VoiceEvaluateStreamConsumer:
  def __init__(self, consumer_name: str | None = None):
    self.consumer_name = (
        consumer_name or VOICE_EVALUATE_CONSUMER_PREFIX + str(id(self))
    )
    self._running = False

  async def start(self) -> None:
    self._running = True
    client = await get_redis()
    try:
      await client.xgroup_create(
          VOICE_EVALUATE_STREAM_KEY,
          VOICE_EVALUATE_GROUP_NAME,
          id="0",
          mkstream=True,
      )
    except Exception:
      pass

    log.info("VoiceEvaluateStreamConsumer started: consumer=%s", self.consumer_name)
    asyncio.create_task(self._consume_loop())

  async def stop(self) -> None:
    self._running = False
    log.info("VoiceEvaluateStreamConsumer stopped")

  async def _consume_loop(self) -> None:
    import redis.asyncio as redis

    client = await get_redis()
    while self._running:
      try:
        messages = await client.xreadgroup(
            groupname=VOICE_EVALUATE_GROUP_NAME,
            consumername=self.consumer_name,
            streams={VOICE_EVALUATE_STREAM_KEY: ">"},
            count=BATCH_SIZE,
            block=POLL_INTERVAL_MS,
        )
        if not messages:
          continue

        for _, msgs in messages:
          for msg_id, fields in msgs:
            await self._process_message(msg_id, fields)
            await client.xack(
                VOICE_EVALUATE_STREAM_KEY,
                VOICE_EVALUATE_GROUP_NAME,
                msg_id,
            )
      except redis.ResponseError as e:
        if "NOSCRIPT" in str(e):
          await client.script_flush()
        log.error("Voice evaluate stream consume error: %s", e)
        await asyncio.sleep(1)

  async def _process_message(self, msg_id: str, fields: dict) -> None:
    session_id = fields.get(FIELD_VOICE_SESSION_ID)
    retry_count_str = fields.get(FIELD_RETRY_COUNT, "0")

    if not session_id:
      log.warning("Invalid voice evaluate message: msgId=%s", msg_id)
      return

    retry_count = int(retry_count_str)
    try:
      await self._do_evaluate(session_id)
    except Exception as e:
      log.error("Voice evaluate failed: sessionId=%s error=%s", session_id, e)
      if retry_count < MAX_RETRY_COUNT:
        await self._requeue(session_id, retry_count + 1)
      else:
        await self._mark_failed(session_id, str(e))

  async def _do_evaluate(self, session_id: str) -> None:
    from app.config.database import _async_session_factory
    from app.models.common import AsyncTaskStatus
    from app.models.interview_dto import InterviewQuestionDTO
    from app.models.resume import ResumeEntity
    from app.models.voice_interview import (
        VoiceInterviewEvaluationEntity,
        VoiceInterviewMessageEntity,
        VoiceInterviewSessionEntity,
    )
    from app.services.interview.unified_evaluation import UnifiedEvaluationService
    from sqlalchemy import select

    started_at = time.perf_counter()
    await self._update_evaluate_status(session_id, AsyncTaskStatus.PROCESSING.value, None)
    log.info("[EVAL_QUEUE] voice_evaluate_start | session_id=%s", session_id)

    async with _async_session_factory() as session:
      entity = await session.get(VoiceInterviewSessionEntity, int(session_id))
      if entity is None:
        log.warning("Voice session not found, skip evaluate: %s", session_id)
        return

      resume_text = ""
      if entity.resume_id:
        resume = await session.get(ResumeEntity, entity.resume_id)
        if resume:
          resume_text = resume.resume_text or ""

      result = await session.execute(
          select(VoiceInterviewMessageEntity)
          .where(VoiceInterviewMessageEntity.session_id == entity.id)
          .order_by(VoiceInterviewMessageEntity.sequence_num, VoiceInterviewMessageEntity.id)
      )
      questions = [
          InterviewQuestionDTO(
              question=message.ai_generated_text or "",
              category=message.phase or "VOICE",
              answer=message.user_recognized_text or "",
          )
          for message in result.scalars().all()
          if message.ai_generated_text and message.user_recognized_text
      ]

      report = await UnifiedEvaluationService().evaluate(
          session_id=session_id,
          resume_text=resume_text,
          questions=questions,
          skill_id=entity.skill_id,
          llm_provider=entity.llm_provider,
      )

      now = get_beijing_now_naive()
      session.add(VoiceInterviewEvaluationEntity(
          session_id=entity.id,
          overall_score=report.overall_score,
          interview_date=to_beijing_naive(entity.start_time) if entity.start_time else None,
          created_at=now,
          interviewer_role=entity.role_type,
          overall_feedback=report.overall_feedback,
          question_evaluations_json=json.dumps(
              [item.model_dump() for item in report.question_evaluations],
              ensure_ascii=False,
          ),
          strengths_json=json.dumps(report.strengths, ensure_ascii=False),
          improvements_json=json.dumps(report.improvements, ensure_ascii=False),
          reference_answers_json=json.dumps(report.reference_answers, ensure_ascii=False),
      ))
      has_errors = any(item.eval_status == "FAILED" for item in report.question_evaluations)
      entity.evaluate_status = (
          AsyncTaskStatus.COMPLETED_WITH_ERRORS.value
          if has_errors else AsyncTaskStatus.COMPLETED.value
      )
      entity.evaluate_error = None
      entity.updated_at = now
      await session.commit()

    log.info(
        "[EVAL_QUEUE] voice_evaluate_complete | session_id=%s status=%s score=%d elapsed=%.3fs",
        session_id, entity.evaluate_status, report.overall_score,
        time.perf_counter() - started_at,
    )

  async def _update_evaluate_status(
      self, session_id: str, status: str, error: str | None
  ) -> None:
    from app.config.database import _async_session_factory
    from app.models.voice_interview import VoiceInterviewSessionEntity

    async with _async_session_factory() as session:
      entity = await session.get(VoiceInterviewSessionEntity, int(session_id))
      if entity:
        entity.evaluate_status = status
        entity.evaluate_error = error[:500] if error else None
        entity.updated_at = get_beijing_now_naive()
        await session.commit()

  async def _requeue(self, session_id: str, retry_count: int) -> None:
    client = await get_redis()
    await client.xadd(
        VOICE_EVALUATE_STREAM_KEY,
        {
            FIELD_VOICE_SESSION_ID: session_id,
            FIELD_RETRY_COUNT: str(retry_count),
        },
        maxlen=STREAM_MAX_LEN,
        approximate=True,
    )
    log.info("Voice evaluate task requeued: sessionId=%s retry=%d", session_id, retry_count)

  async def _mark_failed(self, session_id: str, error: str) -> None:
    from app.models.common import AsyncTaskStatus

    await self._update_evaluate_status(session_id, AsyncTaskStatus.FAILED.value, error)
