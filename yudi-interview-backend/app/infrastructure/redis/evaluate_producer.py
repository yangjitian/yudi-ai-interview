import asyncio
import logging
import time

import redis.asyncio as redis

from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.stream_constants import (
    INTERVIEW_EVALUATE_STREAM_KEY,
    INTERVIEW_EVALUATE_GROUP_NAME,
    INTERVIEW_EVALUATE_CONSUMER_PREFIX,
    FIELD_SESSION_ID,
    FIELD_RETRY_COUNT,
    FIELD_ENQUEUED_AT_NS,
    STREAM_MAX_LEN,
    BATCH_SIZE,
    POLL_INTERVAL_MS,
)

EVALUATE_MAX_RETRY = 1
EVALUATE_RETRY_DELAY_SECONDS = 5


log = logging.getLogger(__name__)

PENDING_MIN_IDLE_MS = 60_000
PENDING_CLAIM_INTERVAL_SECONDS = 30


async def send_evaluate_task(session_id: str) -> None:
  client = await get_redis()
  try:
    message = {
        FIELD_SESSION_ID: session_id,
        FIELD_RETRY_COUNT: "0",
        FIELD_ENQUEUED_AT_NS: str(time.time_ns()),
    }
    stream_id = await client.xadd(
        INTERVIEW_EVALUATE_STREAM_KEY,
        message,
        maxlen=STREAM_MAX_LEN,
        approximate=True,
    )
    log.info(
        "[EVAL_QUEUE] enqueue | session_id=%s stream_id=%s enqueued_at_ns=%s",
        session_id, stream_id, message[FIELD_ENQUEUED_AT_NS],
    )
    log.info("评估任务已发送: sessionId=%s", session_id)
  except Exception as e:
    log.error("发送评估任务失败: sessionId=%s error=%s", session_id, e)
    raise


class EvaluateStreamConsumer:
  def __init__(self, consumer_name: str | None = None):
    self.consumer_name = (
        consumer_name or INTERVIEW_EVALUATE_CONSUMER_PREFIX + str(id(self))
    )
    self._running = False
    self._consume_task: asyncio.Task[None] | None = None

  async def start(self) -> None:
    if self._consume_task and not self._consume_task.done():
      return

    client = await get_redis()
    try:
      await client.xgroup_create(
          INTERVIEW_EVALUATE_STREAM_KEY,
          INTERVIEW_EVALUATE_GROUP_NAME,
          id="0",
          mkstream=True,
      )
    except redis.ResponseError as e:
      if "BUSYGROUP" not in str(e):
        raise

    self._running = True
    log.info("EvaluateStreamConsumer started: consumer=%s", self.consumer_name)
    self._consume_task = asyncio.create_task(self._consume_loop())

  async def stop(self) -> None:
    self._running = False
    task = self._consume_task
    self._consume_task = None
    if task:
      task.cancel()
      try:
        await task
      except asyncio.CancelledError:
        pass
    log.info("EvaluateStreamConsumer stopped: consumer=%s", self.consumer_name)

  async def _consume_loop(self) -> None:
    client = await get_redis()
    last_claim_at = 0.0
    while self._running:
      try:
        now = time.monotonic()
        if now - last_claim_at >= PENDING_CLAIM_INTERVAL_SECONDS:
          await self._claim_pending(client)
          last_claim_at = time.monotonic()

        messages = await client.xreadgroup(
            groupname=INTERVIEW_EVALUATE_GROUP_NAME,
            consumername=self.consumer_name,
            streams={INTERVIEW_EVALUATE_STREAM_KEY: ">"},
            count=BATCH_SIZE,
            block=POLL_INTERVAL_MS,
        )
        if not messages:
          continue

        for stream_name, msgs in messages:
          for msg_id, fields in msgs:
            await self._process_message(client, msg_id, fields)
            await client.xack(
                INTERVIEW_EVALUATE_STREAM_KEY,
                INTERVIEW_EVALUATE_GROUP_NAME,
                msg_id,
            )
      except asyncio.CancelledError:
        raise
      except redis.ResponseError as e:
        log.error("[CONSUMER] 评估消息消费失败: %s", e, exc_info=True)
        await asyncio.sleep(1)
      except Exception as e:
        log.error("[CONSUMER] 评估消费循环异常，将在 1 秒后重试: %s", e, exc_info=True)
        await asyncio.sleep(1)

  async def _claim_pending(self, client: redis.Redis) -> None:
    start_id = "0-0"
    while self._running:
      result = await client.xautoclaim(
          INTERVIEW_EVALUATE_STREAM_KEY,
          INTERVIEW_EVALUATE_GROUP_NAME,
          self.consumer_name,
          PENDING_MIN_IDLE_MS,
          start_id=start_id,
          count=BATCH_SIZE,
      )
      if len(result) < 2:
        return

      next_start_id, messages = result[0], result[1]
      for msg_id, fields in messages:
        log.info("[CONSUMER] 恢复超时评估任务: msgId=%s", msg_id)
        await self._process_message(client, msg_id, fields)
        await client.xack(
            INTERVIEW_EVALUATE_STREAM_KEY,
            INTERVIEW_EVALUATE_GROUP_NAME,
            msg_id,
        )

      if next_start_id == "0-0":
        return
      start_id = next_start_id

  async def _process_message(
      self, client, msg_id: str, fields: dict
  ) -> None:
    session_id = fields.get(FIELD_SESSION_ID)
    retry_count_str = fields.get(FIELD_RETRY_COUNT, "0")

    if not session_id:
      log.warning("Invalid message: missing sessionId")
      return

    retry_count = int(retry_count_str)
    started_at = time.perf_counter()
    enqueued_at_ns = int(fields.get(FIELD_ENQUEUED_AT_NS, time.time_ns()))
    log.info(
        "[EVAL_QUEUE] dequeue | session_id=%s msg_id=%s retry=%d queue_delay_ms=%.1f",
        session_id, msg_id, retry_count, (time.time_ns() - enqueued_at_ns) / 1_000_000,
    )
    log.info(
        "[CONSUMER] 收到评估任务: sessionId=%s msgId=%s retry=%d",
        session_id,
        msg_id,
        retry_count,
    )
    log.info("[CONSUMER] 开始评估: sessionId=%s", session_id)

    try:
      score = await self._do_evaluate(session_id, enqueued_at_ns)
    except Exception as e:
      log.error(
          "[PERF] Eval total_failed | session_id=%s elapsed=%.3fs enqueued_to_failed_ms=%.1f exception_type=%s exception=%r",
          session_id, time.perf_counter() - started_at,
          (time.time_ns() - enqueued_at_ns) / 1_000_000,
          type(e).__name__, e,
      )
      log.error(
          "[CONSUMER] 评估失败: sessionId=%s elapsed=%.3fs error=%s",
          session_id,
          time.perf_counter() - started_at,
          e,
          exc_info=True,
      )
      if retry_count < EVALUATE_MAX_RETRY:
        await asyncio.sleep(EVALUATE_RETRY_DELAY_SECONDS)
        await self._requeue(session_id, retry_count + 1, enqueued_at_ns)
      else:
        await self._mark_failed(session_id, str(e))
    else:
      log.info(
          "[CONSUMER] 评估完成: sessionId=%s elapsed=%.3fs score=%s",
          session_id,
          time.perf_counter() - started_at,
          score if score is not None else "N/A",
      )

  async def _do_evaluate(self, session_id: str, enqueued_at_ns: int | None = None) -> int | None:
    from app.config.database import _async_session_factory
    from app.infrastructure.redis.session_cache import SessionCache
    from app.models.common import AsyncTaskStatus
    from app.services.interview.unified_evaluation import UnifiedEvaluationService
    from app.models.interview_dto import InterviewQuestionDTO, QuestionEvaluation
    from app.repositories.interview_repository import InterviewAnswerRepository
    import json

    from sqlalchemy import select
    from app.models.interview import InterviewSessionEntity

    evaluate_started_at = time.perf_counter()
    await self._update_evaluate_status(session_id, AsyncTaskStatus.PROCESSING.value, None)
    log.info(
        "[PERF] Eval total_start | session_id=%s enqueued_to_start_ms=%.1f",
        session_id,
        (time.time_ns() - enqueued_at_ns) / 1_000_000 if enqueued_at_ns else -1,
    )

    async with _async_session_factory() as session:
      result = await session.execute(
          select(InterviewSessionEntity).where(
              InterviewSessionEntity.session_id == session_id
          )
      )
      entity = result.scalar_one_or_none()
      if entity is None:
        log.warning("会话不存在，跳过评估: %s", session_id)
        return None

      resume_text = ""
      if entity.resume_id:
        from app.models.resume import ResumeEntity
        resume_result = await session.execute(
            select(ResumeEntity).where(ResumeEntity.id == entity.resume_id)
        )
        resume = resume_result.scalar_one_or_none()
        if resume:
          resume_text = resume.resume_text or ""

      questions = []
      if entity.questions_json:
        try:
          raw = json.loads(entity.questions_json)
          questions = [InterviewQuestionDTO(**q) for q in raw]
        except Exception:
          pass

      answer_repo = InterviewAnswerRepository(session)
      answers = await answer_repo.find_by_session(entity.id)
      completed_evaluations: dict[int, QuestionEvaluation] = {}

      for answer in answers:
        index = answer.question_index
        if index is None or not 0 <= index < len(questions):
          continue

        question = questions[index]
        question.answer = answer.user_answer or ""
        if answer.score is not None:
          try:
            key_points = json.loads(answer.key_points_json) if answer.key_points_json else []
          except Exception:
            key_points = []
          completed_evaluations[index] = QuestionEvaluation(
              question_index=index,
              question=question.question,
              category=question.category or "其他",
              score=answer.score,
              feedback=answer.feedback or "",
              reference_answer=answer.reference_answer,
              key_points=key_points,
              eval_status="COMPLETED",
          )
        else:
          answer.score = None
          answer.feedback = None
          answer.reference_answer = None
          answer.key_points_json = None
          answer.eval_status = "EVALUATING"

      await session.commit()

      eval_svc = UnifiedEvaluationService()
      log.info("[PERF] Eval service_start | session_id=%s questions=%d answers=%d", session_id, len(questions), len(answers))
      try:
        report = await eval_svc.evaluate(
            session_id=session_id,
            resume_text=resume_text,
            questions=questions,
            skill_id=entity.skill_id,
            llm_provider=entity.llm_provider,
            completed_evaluations=completed_evaluations,
        )
      except Exception:
        for answer in answers:
          if answer.eval_status == "EVALUATING":
            answer.eval_status = "FAILED"
        await session.commit()
        raise

      completed_indices = set(completed_evaluations)
      for question_eval in report.question_evaluations:
        if question_eval.question_index in completed_indices:
          continue

        question = questions[question_eval.question_index]
        await answer_repo.upsert(
            session_id=entity.id,
            question_index=question_eval.question_index,
            question=question.question,
            category=question.category,
            user_answer=question.answer or "",
        )
        await answer_repo.update_evaluation(
            session_id=entity.id,
            question_index=question_eval.question_index,
            score=question_eval.score,
            feedback=question_eval.feedback,
            reference_answer=question_eval.reference_answer,
            key_points_json=json.dumps(question_eval.key_points, ensure_ascii=False),
            eval_status=question_eval.eval_status or "FAILED",
        )

      for answer in answers:
        if answer.eval_status == "EVALUATING":
          answer.eval_status = "FAILED"

      # 先持久化逐题结果，最终汇总写库失败重试时可跳过已完成题目的 LLM 调用。
      await session.commit()

      entity.overall_score = report.overall_score
      entity.overall_feedback = report.overall_feedback
      entity.status = "EVALUATED"
      entity.strengths_json = json.dumps(report.strengths, ensure_ascii=False)
      entity.improvements_json = json.dumps(report.improvements, ensure_ascii=False)
      entity.reference_answers_json = json.dumps(report.reference_answers, ensure_ascii=False)
      has_errors = any(qe.eval_status == "FAILED" for qe in report.question_evaluations)
      entity.evaluate_status = (
          AsyncTaskStatus.FAILED.value if has_errors
          else AsyncTaskStatus.COMPLETED.value
      )
      await session.commit()
      log.info(
          "[PERF] Eval persisted | session_id=%s status=%s elapsed=%.3fs",
          session_id, entity.evaluate_status, time.perf_counter() - evaluate_started_at,
      )

    cache = SessionCache()
    try:
      if await cache.get_session(session_id):
        await cache.update_status(session_id, "EVALUATED")
    except Exception as e:
      log.warning("更新评估缓存状态失败: sessionId=%s error=%s", session_id, e)

    log.info(
        "[PERF] Eval total_complete | session_id=%s elapsed=%.3fs enqueued_to_complete_ms=%.1f",
        session_id, time.perf_counter() - evaluate_started_at,
        (time.time_ns() - enqueued_at_ns) / 1_000_000 if enqueued_at_ns else -1,
    )
    return report.overall_score

  async def _update_evaluate_status(
      self, session_id: str, status: str, error: str | None
  ) -> None:
    from app.config.database import _async_session_factory
    from sqlalchemy import select
    from app.models.interview import InterviewSessionEntity

    async with _async_session_factory() as session:
      result = await session.execute(
          select(InterviewSessionEntity).where(
              InterviewSessionEntity.session_id == session_id
          )
      )
      entity = result.scalar_one_or_none()
      if entity:
        entity.evaluate_status = status
        entity.evaluate_error = error[:500] if error else None
        await session.commit()

  async def _requeue(self, session_id: str, retry_count: int, enqueued_at_ns: int | None = None) -> None:
    client = await get_redis()
    await client.xadd(
        INTERVIEW_EVALUATE_STREAM_KEY,
        {
            FIELD_SESSION_ID: session_id,
            FIELD_RETRY_COUNT: str(retry_count),
            FIELD_ENQUEUED_AT_NS: str(enqueued_at_ns or time.time_ns()),
        },
        maxlen=STREAM_MAX_LEN,
        approximate=True,
    )
    log.info("评估任务重新入队: sessionId=%s retry=%d", session_id, retry_count)

  async def _mark_failed(self, session_id: str, error: str) -> None:
    from app.models.common import AsyncTaskStatus
    await self._update_evaluate_status(session_id, AsyncTaskStatus.FAILED.value, error)
