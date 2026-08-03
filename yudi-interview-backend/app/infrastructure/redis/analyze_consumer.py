import asyncio
import json
import logging
from datetime import datetime, timezone

import redis.asyncio as redis

from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.stream_constants import (
    RESUME_ANALYZE_STREAM_KEY,
    RESUME_ANALYZE_GROUP_NAME,
    RESUME_ANALYZE_CONSUMER_PREFIX,
    FIELD_RESUME_ID,
    FIELD_CONTENT,
    FIELD_RETRY_COUNT,
    STREAM_MAX_LEN,
    MAX_RETRY_COUNT,
    BATCH_SIZE,
    POLL_INTERVAL_MS,
)


log = logging.getLogger(__name__)


async def send_analyze_task(resume_id: int, content: str) -> None:
  client = await get_redis()
  try:
    message = {
        FIELD_RESUME_ID: str(resume_id),
        FIELD_CONTENT: content,
        FIELD_RETRY_COUNT: "0",
    }
    await client.xadd(
        RESUME_ANALYZE_STREAM_KEY,
        message,
        maxlen=STREAM_MAX_LEN,
        approximate=True,
    )
    log.info("分析任务已发送: resumeId=%d", resume_id)
  except Exception as e:
    log.error("发送分析任务失败: resumeId=%d error=%s", resume_id, e)
    raise


class AnalyzeStreamConsumer:
  def __init__(self, consumer_name: str | None = None):
    self.consumer_name = (
        consumer_name or RESUME_ANALYZE_CONSUMER_PREFIX + str(id(self))
    )
    self._running = False
    self._consume_task: asyncio.Task[None] | None = None

  async def start(self) -> None:
    client = await get_redis()
    try:
      await client.xgroup_create(
          RESUME_ANALYZE_STREAM_KEY,
          RESUME_ANALYZE_GROUP_NAME,
          id="0",
          mkstream=True,
      )
    except redis.ResponseError as e:
      if "BUSYGROUP" not in str(e):
        raise

    self._running = True
    log.info("AnalyzeStreamConsumer started: consumer=%s", self.consumer_name)
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
    log.info("AnalyzeStreamConsumer stopped: consumer=%s", self.consumer_name)

  async def _consume_loop(self) -> None:
    client = await get_redis()
    while self._running:
      try:
        messages = await client.xreadgroup(
            groupname=RESUME_ANALYZE_GROUP_NAME,
            consumername=self.consumer_name,
            streams={RESUME_ANALYZE_STREAM_KEY: ">"},
            count=BATCH_SIZE,
            block=POLL_INTERVAL_MS,
        )
        if not messages:
          continue

        for stream_name, msgs in messages:
          for msg_id, fields in msgs:
            await self._process_message(client, msg_id, fields)
            await client.xack(
                RESUME_ANALYZE_STREAM_KEY,
                RESUME_ANALYZE_GROUP_NAME,
                msg_id,
            )

      except asyncio.CancelledError:
        raise
      except redis.ResponseError as e:
        if "NOSCRIPT" in str(e):
          log.warning("Lua script not found, clearing script cache")
          await client.script_flush()
        else:
          log.error("Stream consume error: %s", e)
          await asyncio.sleep(1)
      except Exception as e:
        log.error("分析消费循环异常，将在 1 秒后重试: %s", e, exc_info=True)
        await asyncio.sleep(1)

  async def _process_message(
      self, client: redis.Redis, msg_id: str, fields: dict
  ) -> None:
    resume_id_str = fields.get(FIELD_RESUME_ID)
    content = fields.get(FIELD_CONTENT)
    retry_count_str = fields.get(FIELD_RETRY_COUNT, "0")

    if not resume_id_str or not content:
      log.warning("Invalid message format: msg_id=%s", msg_id)
      return

    resume_id = int(resume_id_str)
    retry_count = int(retry_count_str)

    log.info("处理分析任务: resumeId=%d retry=%d", resume_id, retry_count)

    try:
      await self._do_analyze(resume_id, content)
    except Exception as e:
      log.error("分析失败: resumeId=%d error=%s", resume_id, e)
      if retry_count < MAX_RETRY_COUNT:
        await self._requeue(resume_id, content, retry_count + 1)
      else:
        await self._mark_failed(resume_id, str(e))

  async def _do_analyze(self, resume_id: int, content: str) -> None:
    from app.services.resume.grading import ResumeGradingService
    from app.models.common import AsyncTaskStatus

    from app.config.database import _async_session_factory

    await self._update_status(resume_id, AsyncTaskStatus.PROCESSING.value, None)

    grading = ResumeGradingService()
    result = await grading.analyze_resume(content)

    async with _async_session_factory() as session:
      from app.services.resume.persistence import ResumePersistenceService
      svc = ResumePersistenceService(session)
      suggestions = [s.model_dump() for s in result.suggestions]
      await svc.save_analysis(
          resume_id=resume_id,
          overall_score=result.overall_score,
          content_score=result.score_detail.content_score,
          structure_score=result.score_detail.structure_score,
          skill_match_score=result.score_detail.skill_match_score,
          expression_score=result.score_detail.expression_score,
          project_score=result.score_detail.project_score,
          summary=result.summary,
          strengths=result.strengths,
          suggestions=suggestions,
      )
      await session.commit()

    await self._update_status(resume_id, AsyncTaskStatus.COMPLETED.value, None)
    log.info("分析完成: resumeId=%d score=%d", resume_id, result.overall_score)

  async def _update_status(
      self, resume_id: int, status: str, error: str | None
  ) -> None:
    from app.config.database import _async_session_factory
    from sqlalchemy import select
    from app.models.resume import ResumeEntity

    async with _async_session_factory() as session:
      result = await session.execute(
          select(ResumeEntity).where(ResumeEntity.id == resume_id)
      )
      entity = result.scalar_one_or_none()
      if entity:
        entity.analyze_status = status
        entity.analyze_error = error[:500] if error else None
        await session.commit()

  async def _requeue(
      self, resume_id: int, content: str, retry_count: int
  ) -> None:
    client = await get_redis()
    await client.xadd(
        RESUME_ANALYZE_STREAM_KEY,
        {
            FIELD_RESUME_ID: str(resume_id),
            FIELD_CONTENT: content,
            FIELD_RETRY_COUNT: str(retry_count),
        },
        maxlen=STREAM_MAX_LEN,
        approximate=True,
    )
    log.info("任务重新入队: resumeId=%d retry=%d", resume_id, retry_count)

  async def _mark_failed(self, resume_id: int, error: str) -> None:
    await self._update_status(resume_id, AsyncTaskStatus.FAILED.value, error)


from app.models.common import AsyncTaskStatus
