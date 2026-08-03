import asyncio
import logging
import time

import redis.asyncio as redis

from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.stream_constants import (
    BATCH_SIZE,
    FIELD_RETRY_COUNT,
    FIELD_VOICE_SESSION_ID,
    POLL_INTERVAL_MS,
    STREAM_MAX_LEN,
    VOICE_EVALUATE_CONSUMER_PREFIX,
    VOICE_EVALUATE_GROUP_NAME,
    VOICE_EVALUATE_STREAM_KEY,
)
from app.utils.timezone_utils import get_beijing_now_naive

EVALUATE_MAX_RETRY = 1
EVALUATE_RETRY_DELAY_SECONDS = 5
PENDING_MIN_IDLE_MS = 60_000
PENDING_CLAIM_INTERVAL_SECONDS = 30

log = logging.getLogger(__name__)


class VoiceEvaluateStreamConsumer:
  def __init__(
      self,
      consumer_name: str | None = None,
      redis_client=None,
      session_factory=None,
  ):
    self.consumer_name = (
        consumer_name or VOICE_EVALUATE_CONSUMER_PREFIX + str(id(self))
    )
    self.redis_client = redis_client
    self.session_factory = session_factory
    self._running = False
    self._consume_task: asyncio.Task[None] | None = None

  async def start(self) -> None:
    if self._consume_task and not self._consume_task.done():
      return
    client = await self._get_client()
    try:
      await client.xgroup_create(
          VOICE_EVALUATE_STREAM_KEY,
          VOICE_EVALUATE_GROUP_NAME,
          id="0",
          mkstream=True,
      )
    except redis.ResponseError as exc:
      if "BUSYGROUP" not in str(exc):
        raise
    self._running = True
    self._consume_task = asyncio.create_task(self._consume_loop())
    log.info("VoiceEvaluateStreamConsumer started: consumer=%s", self.consumer_name)

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
    log.info("VoiceEvaluateStreamConsumer stopped: consumer=%s", self.consumer_name)

  async def _consume_loop(self) -> None:
    client = await self._get_client()
    last_claim_at = 0.0
    while self._running:
      try:
        now = time.monotonic()
        if now - last_claim_at >= PENDING_CLAIM_INTERVAL_SECONDS:
          await self._claim_pending(client)
          last_claim_at = time.monotonic()
        messages = await client.xreadgroup(
            groupname=VOICE_EVALUATE_GROUP_NAME,
            consumername=self.consumer_name,
            streams={VOICE_EVALUATE_STREAM_KEY: ">"},
            count=BATCH_SIZE,
            block=POLL_INTERVAL_MS,
        )
        for _, entries in messages or []:
          for message_id, fields in entries:
            await self._process_message(client, message_id, fields)
            await self._ack(client, message_id)
      except asyncio.CancelledError:
        raise
      except Exception:
        log.exception("语音面试评估消费循环异常，将在 1 秒后重试")
        await asyncio.sleep(1)

  async def _claim_pending(self, client) -> None:
    start_id = "0-0"
    while self._running:
      result = await client.xautoclaim(
          VOICE_EVALUATE_STREAM_KEY,
          VOICE_EVALUATE_GROUP_NAME,
          self.consumer_name,
          PENDING_MIN_IDLE_MS,
          start_id=start_id,
          count=BATCH_SIZE,
      )
      if len(result) < 2:
        return
      next_start_id, messages = result[0], result[1]
      for message_id, fields in messages:
        await self._process_message(client, message_id, fields)
        await self._ack(client, message_id)
      if next_start_id == "0-0":
        return
      start_id = next_start_id

  async def _process_message(self, client, message_id: str, fields: dict) -> None:
    raw_session_id = fields.get(FIELD_VOICE_SESSION_ID)
    if not raw_session_id:
      log.warning("语音评估消息缺少 voiceSessionId: messageId=%s", message_id)
      return
    try:
      session_id = int(raw_session_id)
      retry_count = int(fields.get(FIELD_RETRY_COUNT, "0"))
    except (TypeError, ValueError):
      log.warning("语音评估消息格式错误: messageId=%s", message_id)
      return

    try:
      await self._do_evaluate(session_id)
    except Exception as exc:
      log.exception("语音面试评估失败: sessionId=%s", session_id)
      if retry_count < EVALUATE_MAX_RETRY:
        await asyncio.sleep(EVALUATE_RETRY_DELAY_SECONDS)
        await client.xadd(
            VOICE_EVALUATE_STREAM_KEY,
            {
                FIELD_VOICE_SESSION_ID: str(session_id),
                FIELD_RETRY_COUNT: str(retry_count + 1),
            },
            maxlen=STREAM_MAX_LEN,
            approximate=True,
        )
      else:
        await self._update_status(session_id, "FAILED", str(exc))

  async def _do_evaluate(self, session_id: int) -> int | None:
    from app.repositories.voice_evaluation_repository import VoiceEvaluationRepository
    from app.repositories.voice_message_repository import VoiceMessageRepository
    from app.repositories.voice_session_repository import VoiceSessionRepository
    from app.services.voice.evaluation_service import VoiceInterviewEvaluationService

    async with self._get_session_factory()() as db:
      session_repo = VoiceSessionRepository(db)
      entity = await session_repo.find_by_id(session_id)
      if entity is None:
        log.warning("语音面试会话已删除，跳过评估: sessionId=%s", session_id)
        return None
      if entity.evaluate_status == "COMPLETED":
        return None

      entity.evaluate_status = "PROCESSING"
      entity.evaluate_error = None
      entity.updated_at = get_beijing_now_naive()
      await session_repo.save(entity)
      await session_repo.commit()

      service = VoiceInterviewEvaluationService(
          session_repo=session_repo,
          message_repo=VoiceMessageRepository(db),
          evaluation_repo=VoiceEvaluationRepository(db),
      )
      score = await service.generate_evaluation(session_id)
      entity.evaluate_status = "COMPLETED"
      entity.evaluate_error = None
      entity.updated_at = get_beijing_now_naive()
      await session_repo.save(entity)
      await session_repo.commit()
      return score

  async def _update_status(
      self, session_id: int, status: str, error: str | None
  ) -> None:
    from app.repositories.voice_session_repository import VoiceSessionRepository

    async with self._get_session_factory()() as db:
      repository = VoiceSessionRepository(db)
      entity = await repository.find_by_id(session_id)
      if entity is None:
        return
      entity.evaluate_status = status
      entity.evaluate_error = error[:500] if error else None
      entity.updated_at = get_beijing_now_naive()
      await repository.save(entity)
      await repository.commit()

  async def _ack(self, client, message_id: str) -> None:
    await client.xack(
        VOICE_EVALUATE_STREAM_KEY,
        VOICE_EVALUATE_GROUP_NAME,
        message_id,
    )

  async def _get_client(self):
    return self.redis_client or await get_redis()

  def _get_session_factory(self):
    if self.session_factory is not None:
      return self.session_factory
    from app.config.database import _async_session_factory
    return _async_session_factory
