import asyncio
import logging
import time
from datetime import datetime, timedelta
from uuid import uuid4

import redis.asyncio as redis

from app.config.database import _async_session_factory
from app.config.settings import get_settings
from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.stream_constants import (
    BATCH_SIZE,
    FIELD_KB_ID,
    FIELD_RETRY_COUNT,
    FIELD_TASK_ID,
    KB_QUESTION_GEN_CONSUMER_PREFIX,
    KB_QUESTION_GEN_GROUP_NAME,
    KB_QUESTION_GEN_STREAM_KEY,
    MAX_RETRY_COUNT,
    PENDING_CLAIM_BATCH_SIZE,
    PENDING_IDLE_TIMEOUT_MS,
    POLL_INTERVAL_MS,
    STREAM_MAX_LEN,
)
from app.models.kb_dto import QuestionGenerationConfig, QuestionGenerationStatus
from app.repositories.kb_repository import KbRepository, KnowledgeBaseQuestionRepository
from app.services.kb.question_generation import KnowledgeBaseQuestionGenerationService
from app.services.kb.question_generation_state import QuestionGenerationStateService
from app.utils.timezone_utils import get_beijing_now_naive


log = logging.getLogger(__name__)
_running_generation_tasks: dict[str, asyncio.Task[bool]] = {}


def cancel_running_question_generation(task_id: str) -> bool:
  task = _running_generation_tasks.get(task_id)
  if task is None or task.done():
    return False
  task.cancel()
  return True


class QuestionGenerationTaskRuntime:
  def __init__(
      self,
      session_factory=_async_session_factory,
      generation_service: KnowledgeBaseQuestionGenerationService | None = None,
  ):
    self.session_factory = session_factory
    self.generation_service = generation_service or KnowledgeBaseQuestionGenerationService(
        session_factory=session_factory
    )

  async def claim(self, kb_id: int, task_id: str) -> bool:
    async with self.session_factory() as session:
      return await self._state_service(session).claim(kb_id, task_id)

  async def get_config(self, kb_id: int, task_id: str) -> QuestionGenerationConfig:
    async with self.session_factory() as session:
      return await self._state_service(session).get_config(kb_id, task_id)

  async def generate(
      self,
      kb_id: int,
      task_id: str,
      config: QuestionGenerationConfig,
  ) -> bool:
    return await self.generation_service.execute_generation(kb_id, task_id, config)

  async def retry(self, kb_id: int, task_id: str) -> bool:
    async with self.session_factory() as session:
      return await self._state_service(session).retry(kb_id, task_id)

  async def fail(
      self,
      kb_id: int,
      task_id: str,
      error_message: str | None = None,
  ) -> bool:
    async with self.session_factory() as session:
      return await self._state_service(session).fail(kb_id, task_id, error_message)

  async def cancel(self, kb_id: int, task_id: str) -> None:
    async with self.session_factory() as session:
      await self._state_service(session).cancel(kb_id, task_id)

  async def find_stale(
      self,
      status: QuestionGenerationStatus,
      threshold: datetime,
  ) -> list:
    async with self.session_factory() as session:
      return await KbRepository(session).find_stale_question_generation_tasks(
          status.value,
          threshold,
      )

  async def touch_queued(
      self,
      kb_id: int,
      task_id: str,
      threshold: datetime,
  ) -> bool:
    async with self.session_factory() as session:
      return await self._state_service(session).touch_queued_for_recovery(
          kb_id,
          task_id,
          threshold,
      )

  async def reset_stale_processing(
      self,
      kb_id: int,
      task_id: str,
      threshold: datetime,
  ) -> bool:
    async with self.session_factory() as session:
      return await self._state_service(session).reset_stale_processing(
          kb_id,
          task_id,
          threshold,
      )

  @staticmethod
  def _state_service(session) -> QuestionGenerationStateService:
    return QuestionGenerationStateService(
        KbRepository(session),
        KnowledgeBaseQuestionRepository(session),
    )


class QuestionGenStreamProducer:
  def __init__(self, redis_client=None, runtime: QuestionGenerationTaskRuntime | None = None):
    self.redis_client = redis_client
    self.runtime = runtime or QuestionGenerationTaskRuntime()

  async def send_generate_task(
      self,
      kb_id: int,
      task_id: str,
      retry_count: int = 0,
  ) -> bool:
    client = self.redis_client or await get_redis()
    message = {
        FIELD_KB_ID: str(kb_id),
        FIELD_TASK_ID: task_id,
        FIELD_RETRY_COUNT: str(retry_count),
    }
    try:
      await client.xadd(
          KB_QUESTION_GEN_STREAM_KEY,
          message,
          maxlen=STREAM_MAX_LEN,
          approximate=True,
      )
      log.info(
          "题目生成任务已发送: kbId=%d taskId=%s retryCount=%d",
          kb_id,
          task_id,
          retry_count,
      )
      return True
    except Exception as exc:
      log.error(
          "发送题目生成任务失败: kbId=%d taskId=%s error=%s",
          kb_id,
          task_id,
          exc,
          exc_info=True,
      )
      await self.runtime.fail(kb_id, task_id, f"任务入队失败: {exc}")
      return False


class QuestionGenStreamConsumer:
  def __init__(
      self,
      redis_client=None,
      runtime: QuestionGenerationTaskRuntime | None = None,
      producer: QuestionGenStreamProducer | None = None,
      consumer_name: str | None = None,
      generation_timeout_seconds: float | None = None,
  ):
    self.redis_client = redis_client
    self.runtime = runtime or QuestionGenerationTaskRuntime()
    self.producer = producer or QuestionGenStreamProducer(
        redis_client=redis_client,
        runtime=self.runtime,
    )
    self.consumer_name = consumer_name or (
        KB_QUESTION_GEN_CONSUMER_PREFIX + uuid4().hex[:8]
    )
    self.generation_timeout_seconds = generation_timeout_seconds or float(
        get_settings().interview.question_generation_timeout_seconds
    )
    self._running = False
    self._consume_task: asyncio.Task[None] | None = None

  async def start(self) -> None:
    if self._consume_task and not self._consume_task.done():
      return
    client = self.redis_client or await get_redis()
    try:
      await client.xgroup_create(
          KB_QUESTION_GEN_STREAM_KEY,
          KB_QUESTION_GEN_GROUP_NAME,
          id="0",
          mkstream=True,
      )
    except redis.ResponseError as exc:
      if "BUSYGROUP" not in str(exc):
        raise
    self._running = True
    self._consume_task = asyncio.create_task(self._consume_loop())
    log.info("QuestionGenStreamConsumer started: consumer=%s", self.consumer_name)

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

  async def _consume_loop(self) -> None:
    client = self.redis_client or await get_redis()
    last_claim_at = 0.0
    while self._running:
      try:
        now = time.monotonic()
        if now - last_claim_at >= PENDING_IDLE_TIMEOUT_MS / 1000:
          await self._claim_pending(client)
          last_claim_at = time.monotonic()
        messages = await client.xreadgroup(
            groupname=KB_QUESTION_GEN_GROUP_NAME,
            consumername=self.consumer_name,
            streams={KB_QUESTION_GEN_STREAM_KEY: ">"},
            count=BATCH_SIZE,
            block=POLL_INTERVAL_MS,
        )
        for _stream_name, stream_messages in messages or []:
          for message_id, fields in stream_messages:
            await self._process_message(client, message_id, fields)
      except asyncio.CancelledError:
        raise
      except Exception:
        log.exception("题目生成消费循环异常，将在 1 秒后重试")
        await asyncio.sleep(1)

  async def _claim_pending(self, client) -> None:
    result = await client.xautoclaim(
        KB_QUESTION_GEN_STREAM_KEY,
        KB_QUESTION_GEN_GROUP_NAME,
        self.consumer_name,
        PENDING_IDLE_TIMEOUT_MS,
        start_id="0-0",
        count=PENDING_CLAIM_BATCH_SIZE,
    )
    if len(result) < 2:
      return
    for message_id, fields in result[1]:
      await self._process_message(client, message_id, fields)

  async def _process_message(self, client, message_id: str, fields: dict) -> None:
    kb_id_text = fields.get(FIELD_KB_ID)
    task_id = fields.get(FIELD_TASK_ID)
    try:
      if kb_id_text is None or task_id is None:
        raise ValueError("缺少 kbId 或 taskId")
      kb_id = int(kb_id_text)
    except (TypeError, ValueError):
      log.warning("题目生成消息格式错误，确认并丢弃: messageId=%s", message_id)
      await self._ack(client, message_id)
      return
    try:
      retry_count = int(fields.get(FIELD_RETRY_COUNT, "0"))
    except (TypeError, ValueError):
      retry_count = 0

    try:
      if not await self.runtime.claim(kb_id, task_id):
        return
      config = await self.runtime.get_config(kb_id, task_id)
      task_timeout_seconds = (
          self.generation_timeout_seconds * max(1, config.questionCount)
      )
      log.info(
          "题目生成任务开始: kbId=%d taskId=%s timeout=%.1fs",
          kb_id,
          task_id,
          task_timeout_seconds,
      )
      generation_task = asyncio.create_task(
          self.runtime.generate(kb_id, task_id, config)
      )
      _running_generation_tasks[task_id] = generation_task
      try:
        await asyncio.wait_for(
            generation_task,
            timeout=task_timeout_seconds,
        )
      except asyncio.CancelledError:
        if asyncio.current_task() and asyncio.current_task().cancelling():
          raise
        await self.runtime.cancel(kb_id, task_id)
        log.info(
            "题目生成任务已由用户取消: kbId=%d taskId=%s",
            kb_id,
            task_id,
        )
        return
      except TimeoutError as exc:
        raise TimeoutError(
            f"题目生成超时（{task_timeout_seconds:g} 秒）"
        ) from exc
      finally:
        if _running_generation_tasks.get(task_id) is generation_task:
          _running_generation_tasks.pop(task_id, None)
    except Exception as exc:
      log.error(
          "题目生成任务失败: kbId=%d taskId=%s retryCount=%d",
          kb_id,
          task_id,
          retry_count,
          exc_info=True,
      )
      if retry_count < MAX_RETRY_COUNT:
        if await self.runtime.retry(kb_id, task_id):
          await self.producer.send_generate_task(
              kb_id,
              task_id,
              retry_count + 1,
          )
      else:
        await self.runtime.fail(kb_id, task_id, str(exc))
    finally:
      await self._ack(client, message_id)

  @staticmethod
  async def _ack(client, message_id: str) -> None:
    await client.xack(
        KB_QUESTION_GEN_STREAM_KEY,
        KB_QUESTION_GEN_GROUP_NAME,
        message_id,
    )


class QuestionGenerationRecoveryScheduler:
  INTERVAL_SECONDS = 60
  QUEUED_STALE_MINUTES = 2
  PROCESSING_STALE_MINUTES = 20

  def __init__(
      self,
      runtime: QuestionGenerationTaskRuntime | None = None,
      producer: QuestionGenStreamProducer | None = None,
  ):
    self.runtime = runtime or QuestionGenerationTaskRuntime()
    self.producer = producer or QuestionGenStreamProducer(runtime=self.runtime)
    self._running = False
    self._task: asyncio.Task[None] | None = None

  async def start(self) -> None:
    if self._task and not self._task.done():
      return
    self._running = True
    self._task = asyncio.create_task(self._loop())

  async def stop(self) -> None:
    self._running = False
    task = self._task
    self._task = None
    if task:
      task.cancel()
      try:
        await task
      except asyncio.CancelledError:
        pass

  async def _loop(self) -> None:
    await asyncio.sleep(self.INTERVAL_SECONDS)
    while self._running:
      try:
        await self.run_once()
      except Exception:
        log.exception("恢复题目生成超时任务失败")
      await asyncio.sleep(self.INTERVAL_SECONDS)

  async def run_once(self, now: datetime | None = None) -> None:
    current = now or get_beijing_now_naive()
    queued_threshold = current - timedelta(minutes=self.QUEUED_STALE_MINUTES)
    processing_threshold = current - timedelta(
        minutes=self.PROCESSING_STALE_MINUTES
    )
    queued_tasks = await self.runtime.find_stale(
        QuestionGenerationStatus.QUEUED,
        queued_threshold,
    )
    for task in queued_tasks:
      if task.question_gen_task_id and await self.runtime.touch_queued(
          task.id,
          task.question_gen_task_id,
          queued_threshold,
      ):
        await self.producer.send_generate_task(task.id, task.question_gen_task_id)

    processing_tasks = await self.runtime.find_stale(
        QuestionGenerationStatus.PROCESSING,
        processing_threshold,
    )
    for task in processing_tasks:
      if task.question_gen_task_id and await self.runtime.reset_stale_processing(
          task.id,
          task.question_gen_task_id,
          processing_threshold,
      ):
        await self.producer.send_generate_task(task.id, task.question_gen_task_id)
