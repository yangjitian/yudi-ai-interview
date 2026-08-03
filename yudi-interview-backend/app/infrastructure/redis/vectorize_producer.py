import asyncio
import logging

from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.stream_constants import (
    KB_VECTORIZE_STREAM_KEY,
    KB_VECTORIZE_GROUP_NAME,
    KB_VECTORIZE_CONSUMER_PREFIX,
    FIELD_KB_ID,
    FIELD_CONTENT,
    FIELD_RETRY_COUNT,
    STREAM_MAX_LEN,
    MAX_RETRY_COUNT,
    BATCH_SIZE,
    POLL_INTERVAL_MS,
)


log = logging.getLogger(__name__)


async def send_vectorize_task(kb_id: int, content: str) -> None:
  client = await get_redis()
  try:
    message = {
        FIELD_KB_ID: str(kb_id),
        FIELD_CONTENT: content,
        FIELD_RETRY_COUNT: "0",
    }
    await client.xadd(
        KB_VECTORIZE_STREAM_KEY,
        message,
        maxlen=STREAM_MAX_LEN,
        approximate=True,
    )
    log.info("向量化任务已发? kbId=%d", kb_id)
  except Exception as e:
    log.error("发送向量化任务失败: kbId=%d error=%s", kb_id, e)
    raise


class VectorizeStreamConsumer:
  def __init__(self, consumer_name: str | None = None):
    self.consumer_name = (
        consumer_name or KB_VECTORIZE_CONSUMER_PREFIX + str(id(self))
    )
    self._running = False
    self._consume_task: asyncio.Task[None] | None = None

  async def start(self) -> None:
    client = await get_redis()
    try:
      await client.xgroup_create(
          KB_VECTORIZE_STREAM_KEY,
          KB_VECTORIZE_GROUP_NAME,
          id="0",
          mkstream=True,
      )
    except Exception:
      pass
    self._running = True
    log.info("VectorizeStreamConsumer started: %s", self.consumer_name)
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

  async def _consume_loop(self) -> None:
    import redis.asyncio as redis
    client = await get_redis()
    while self._running:
      try:
        messages = await client.xreadgroup(
            groupname=KB_VECTORIZE_GROUP_NAME,
            consumername=self.consumer_name,
            streams={KB_VECTORIZE_STREAM_KEY: ">"},
            count=BATCH_SIZE,
            block=POLL_INTERVAL_MS,
        )
        if not messages:
          continue
        for stream_name, msgs in messages:
          for msg_id, fields in msgs:
            await self._process_message(client, msg_id, fields)
            await client.xack(
                KB_VECTORIZE_STREAM_KEY,
                KB_VECTORIZE_GROUP_NAME,
                msg_id,
            )
      except asyncio.CancelledError:
        raise
      except redis.ResponseError as e:
        if "NOSCRIPT" in str(e):
          await client.script_flush()
        log.error("Vectorize consume error: %s", e)
        await asyncio.sleep(1)
      except Exception as e:
        log.error("向量化消费循环异常，将在 1 秒后重试: %s", e, exc_info=True)
        await asyncio.sleep(1)

  async def _process_message(self, client, msg_id: str, fields: dict) -> None:
    kb_id_str = fields.get(FIELD_KB_ID)
    content = fields.get(FIELD_CONTENT)
    retry_str = fields.get(FIELD_RETRY_COUNT, "0")
    if not kb_id_str or not content:
      return
    kb_id = int(kb_id_str)
    retry = int(retry_str)
    try:
      await self._mark_processing(kb_id)
      await self._do_vectorize(kb_id, content)
    except Exception as e:
      log.error("向量化失? kbId=%d error=%s", kb_id, e)
      if retry < MAX_RETRY_COUNT:
        await self._requeue(kb_id, content, retry + 1)
      else:
        await self._mark_failed(kb_id, str(e))

  async def _do_vectorize(self, kb_id: int, content: str) -> None:
    from app.models.knowledge_base import VectorStatus

    if not content or not isinstance(content, str) or not content.strip():
      raise ValueError(f"向量化内容无效: kbId={kb_id}")

    chunks = self._chunk_content(content)
    if not chunks:
      raise ValueError(f"文档分块结果为空: kbId={kb_id}")
    chunk_count = len(chunks)

    embeddings = await self._generate_embeddings(chunks)
    await self._save_vectors(kb_id, chunks, embeddings)

    from app.config.database import _async_session_factory
    async with _async_session_factory() as session:
      from sqlalchemy import update
      from app.models.knowledge_base import KnowledgeBaseEntity
      await session.execute(
          update(KnowledgeBaseEntity)
          .where(KnowledgeBaseEntity.id == kb_id)
          .values(
               vector_status=VectorStatus.COMPLETED.value,
               chunk_count=chunk_count,
               vector_error=None,
          )
      )
      await session.commit()

    log.info("向量化完? kbId=%d chunkCount=%d", kb_id, chunk_count)

  async def _generate_embeddings(self, chunks: list[str]) -> list[list[float]]:
    from app.infrastructure.ai.embedding_client import EmbeddingClient

    return await EmbeddingClient().embed_batch(chunks)

  async def _save_vectors(
      self, kb_id: int, chunks: list[str], embeddings: list[list[float]]
  ) -> None:
    from uuid import uuid4
    from app.config.database import _async_session_factory
    from app.models.knowledge_base import VectorStoreEntity

    async with _async_session_factory() as session:
        for chunk_content, embedding in zip(chunks, embeddings):
            session.add(VectorStoreEntity(
                id=str(uuid4()),
                content=chunk_content,
                metadata_={"kb_id": str(kb_id)},
                embedding=embedding,
            ))

        await session.commit()
        log.info("向量写入完成: kbId=%d chunks=%d", kb_id, len(chunks))

  def _chunk_content(self, content: str, chunk_size: int = 800) -> list[str]:
    """将文本按段落边界分块（对应 Java TokenTextSplitter，约 800 tokens/块）。"""
    if not content or not content.strip():
      return []
    paragraphs = content.split("\n\n")
    chunks = []
    current = []
    current_len = 0
    for para in paragraphs:
      stripped = para.strip()
      if not stripped:
        continue
      # 单段落超过 chunk_size 时强制切分
      if len(stripped) > chunk_size:
        if current:
          chunks.append("\n\n".join(current))
          current = []
          current_len = 0
        for i in range(0, len(stripped), chunk_size):
          sub = stripped[i:i + chunk_size].strip()
          if sub:
            chunks.append(sub)
        continue
      if current_len + len(stripped) > chunk_size and current:
        chunks.append("\n\n".join(current))
        current = []
        current_len = 0
      current.append(stripped)
      current_len += len(stripped)
    if current:
      chunks.append("\n\n".join(current))
    return [c for c in chunks if c.strip()]

  async def _requeue(self, kb_id: int, content: str, retry: int) -> None:
    client = await get_redis()
    await client.xadd(
        KB_VECTORIZE_STREAM_KEY,
        {FIELD_KB_ID: str(kb_id), FIELD_CONTENT: content, FIELD_RETRY_COUNT: str(retry)},
        maxlen=STREAM_MAX_LEN,
        approximate=True,
    )

  async def _mark_failed(self, kb_id: int, error: str) -> None:
    from app.config.database import _async_session_factory
    from sqlalchemy import update
    from app.models.knowledge_base import KnowledgeBaseEntity, VectorStatus

    async with _async_session_factory() as session:
      await session.execute(
          update(KnowledgeBaseEntity)
          .where(KnowledgeBaseEntity.id == kb_id)
          .values(
              vector_status=VectorStatus.FAILED.value,
              vector_error=error[:500],
          )
      )
      await session.commit()

  async def _mark_processing(self, kb_id: int) -> None:
    from app.config.database import _async_session_factory
    from sqlalchemy import update
    from app.models.knowledge_base import KnowledgeBaseEntity, VectorStatus

    async with _async_session_factory() as session:
      await session.execute(
          update(KnowledgeBaseEntity)
          .where(KnowledgeBaseEntity.id == kb_id)
          .values(
              vector_status=VectorStatus.PROCESSING.value,
              vector_error=None,
          )
      )
      await session.commit()
