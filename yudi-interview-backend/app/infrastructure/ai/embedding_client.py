import asyncio

from app.infrastructure.ai.provider_registry import get_embedding_client
from app.models.knowledge_base import EMBEDDING_DIMENSION


class EmbeddingClient:
  # 阿里云 DashScope Embedding API 批量大小限制（与 Java 版 MAX_BATCH_SIZE 一致）
  MAX_BATCH_SIZE = 10

  async def embed_text(self, text: str) -> list[float]:
    client = await get_embedding_client()
    if hasattr(client, "aembed_query"):
      embedding = await client.aembed_query(text)
    else:
      embedding = await asyncio.to_thread(client.embed_query, text)
    self._validate_dimension(embedding)
    return list(embedding)

  async def embed_batch(self, texts: list[str]) -> list[list[float]]:
    if not texts:
      return []

    # 过滤非字符串和空值，防止 DashScope API 报 400
    valid_texts = [t for t in texts if isinstance(t, str) and t.strip()]
    if not valid_texts:
      raise ValueError("无有效的文本块可供向量化")

    client = await get_embedding_client()
    embeddings: list[list[float]] = []
    for start in range(0, len(valid_texts), self.MAX_BATCH_SIZE):
      batch = valid_texts[start:start + self.MAX_BATCH_SIZE]
      if hasattr(client, "aembed_documents"):
        result = await client.aembed_documents(batch)
      else:
        result = await asyncio.to_thread(client.embed_documents, batch)
      for embedding in result:
        self._validate_dimension(embedding)
        embeddings.append(list(embedding))
    return embeddings

  @staticmethod
  def _validate_dimension(embedding: list[float]) -> None:
    if len(embedding) != EMBEDDING_DIMENSION:
      raise ValueError(
          f"Embedding 维度不匹配: 期望 {EMBEDDING_DIMENSION}，实际 {len(embedding)}"
      )
