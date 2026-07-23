import asyncio

from app.infrastructure.ai.provider_registry import get_embedding_client
from app.models.knowledge_base import EMBEDDING_DIMENSION


class EmbeddingClient:
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

    client = await get_embedding_client()
    embeddings: list[list[float]] = []
    for start in range(0, len(texts), 25):
      batch = texts[start:start + 25]
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
