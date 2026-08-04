import logging
from uuid import uuid4

from app.utils.file_hash import compute_sha256
from app.infrastructure.storage.file_storage import generate_storage_key, upload_file, delete_file, generate_public_url
from app.models.knowledge_base import (
    KnowledgeBaseEntity,
    VectorStatus,
    VectorStoreEntity,
)
from app.repositories.kb_repository import KbRepository
from app.infrastructure.parser.document_parser import parse_document
from app.infrastructure.ai.embedding_client import EmbeddingClient
from app.services.document_processor import DocumentProcessor


log = logging.getLogger(__name__)


def _get_storage_bucket() -> str:
  from app.config.settings import get_settings
  return get_settings().storage.bucket


class KnowledgeBaseUploadService:
  def __init__(self, kb_repo: KbRepository):
    self.kb_repo = kb_repo
    self._embedding_client = EmbeddingClient()
    self._document_processor = DocumentProcessor()
    self._chunk_size = 500
    self._chunk_overlap = 50

  async def upload(
      self,
      content: bytes,
      filename: str,
      name: str,
      content_type: str | None,
      category: str | None,
  ) -> dict:
    """
    上传知识库文档（单文件模式，与 Java 版一致）。

    流程：
    1. 计算文件 hash 检测重复
    2. 创建知识库记录
    3. 上传到 S3
    4. 解析文本
    5. 同步向量化（写入 vector_store 表）
    6. commit
    """
    file_hash = compute_sha256(content)
    file_size = len(content)

    # 检测重复（按 hash）
    existing = await self.kb_repo.find_by_hash(file_hash)
    if existing:
      log.info("检测到重复知识库文档: kbId=%d", existing.id)
      return {"id": existing.id, "duplicate": True}

    # 创建知识库实体
    kb_entity = KnowledgeBaseEntity(
        file_hash=file_hash,
        name=name,
        original_filename=filename,
        file_size=file_size,
        content_type=content_type,
        category=category,
        vector_status=VectorStatus.PENDING.value,
    )
    kb_saved = await self.kb_repo.save(kb_entity)

    # 上传到 S3
    storage_key = generate_storage_key("knowledgebase", filename)
    await upload_file(content, storage_key, content_type)
    log.info(
        "[upload] put_object success bucket=%s key=%s",
        _get_storage_bucket(),
        storage_key,
    )
    kb_saved.storage_key = storage_key
    kb_saved.storage_url = generate_public_url(storage_key)

    # 解析文本
    resume_text = await parse_document(content, content_type, filename)

    # 同步向量化（写入 vector_store 表）
    try:
      chunk_count = await self._vectorize_text(resume_text, kb_saved.id)
      kb_saved.vector_status = VectorStatus.COMPLETED.value
      kb_saved.chunk_count = chunk_count
    except Exception as exc:
      kb_saved.vector_status = VectorStatus.FAILED.value
      kb_saved.vector_error = str(exc)[:500]
      log.error("[upload] 向量化失败 kbId=%d: %s", kb_saved.id, exc)
      await self.kb_repo.session.flush()
      raise

    await self.kb_repo.session.flush()
    await self.kb_repo.session.commit()
    return {
        "id": kb_saved.id,
        "duplicate": False,
        "name": kb_saved.name,
    }

  async def _vectorize_text(self, text: str, kb_id: int) -> int:
    """将文本分块并向量化，写入 vector_store 表（与 Java Spring AI 一致）。"""
    if not text or not text.strip():
      raise ValueError("文档中没有可向量化的内容")

    chunks = self._document_processor.split_into_chunks(
        text, self._chunk_size, self._chunk_overlap
    )
    if not chunks:
      raise ValueError("文档中没有可向量化的内容")

    embeddings = await self._embedding_client.embed_batch(chunks)
    if not embeddings:
      raise ValueError("Embedding 返回结果为空")

    # embed_batch 内部会过滤空块，用实际返回数量写入
    valid_chunks = [c for c in chunks if isinstance(c, str) and c.strip()]
    self.kb_repo.session.add_all([
        VectorStoreEntity(
            id=str(uuid4()),
            content=valid_chunks[idx],
            metadata_={"kb_id": str(kb_id)},
            embedding=embeddings[idx],
        )
        for idx in range(len(embeddings))
    ])
    await self.kb_repo.session.flush()
    return len(embeddings)

  async def delete_kb(self, kb_id: int) -> None:
    from sqlalchemy import delete
    kb_entity = await self.kb_repo.find_by_id(kb_id)
    if kb_entity is None:
      return

    # 删除 RAG 会话中的知识库关联
    await self._remove_rag_session_associations(kb_id)

    # 删除向量数据（vector_store 表中 metadata->>'kb_id' 匹配的记录）
    try:
      await self.kb_repo.session.execute(
          delete(VectorStoreEntity).where(
              VectorStoreEntity.metadata_["kb_id"].astext == str(kb_id)
          )
      )
    except Exception as e:
      log.warning("Delete vectors failed for KB %d: %s", kb_id, e)

    # 删除知识库文件
    if kb_entity.storage_key:
      try:
        await delete_file(kb_entity.storage_key)
      except Exception as e:
        log.warning("S3 delete failed for KB %d: %s", kb_id, e)

    await self.kb_repo.delete(kb_id)

  async def _remove_rag_session_associations(self, kb_id: int) -> None:
    from sqlalchemy import select
    from app.models.knowledge_base import RagChatSessionEntity
    from sqlalchemy.orm import selectinload

    result = await self.kb_repo.session.execute(
        select(RagChatSessionEntity)
        .options(selectinload(RagChatSessionEntity.knowledge_bases))
    )
    sessions = result.scalars().all()

    for session in sessions:
      if any(kb.id == kb_id for kb in session.knowledge_bases):
        session.knowledge_bases = [kb for kb in session.knowledge_bases if kb.id != kb_id]
        log.debug("Removed KB %d from RAG session %d", kb_id, session.id)

    if sessions:
      log.info("Removed KB %d from %d RAG sessions", kb_id, len(sessions))



  @staticmethod
  def _content_type(file_type: str) -> str:
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "md": "text/markdown",
        "txt": "text/plain",
    }.get(file_type, "application/octet-stream")
