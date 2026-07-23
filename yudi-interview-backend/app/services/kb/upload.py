import logging
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import selectinload

from app.utils.file_hash import compute_sha256
from app.infrastructure.storage.file_storage import generate_storage_key, upload_file, delete_file, generate_public_url
from app.models.knowledge_base import (
    KnowledgeBaseEntity,
    KnowledgeBaseStatus,
    KnowledgeChunkEntity,
    KnowledgeDocumentEntity,
    VectorStatus,
)
from app.repositories.kb_repository import KbRepository
from app.infrastructure.parser.document_parser import parse_document
from app.infrastructure.redis.vectorize_producer import send_vectorize_task
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
    上传知识库文档（多文档模式）。

    流程：
    1. 计算文件 hash 检测重复
    2. 创建知识库记录
    3. 创建文档记录并 flush 以获取主键
    4. 上传到 S3
    5. 解析文本
    6. 同步向量化
    7. commit
    """
    file_hash = compute_sha256(content)
    file_size = len(content)
    file_type = Path(filename).suffix.lower().lstrip(".")

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
        doc_count=0,
        status=KnowledgeBaseStatus.ACTIVE.value,
    )
    kb_saved = await self.kb_repo.save(kb_entity)

    # 创建文档记录
    doc_uuid = str(uuid4())
    safe_filename = Path(filename).name
    storage_key = generate_storage_key("knowledgebase", filename)

    doc_entity = KnowledgeDocumentEntity(
        doc_id=doc_uuid,
        kb_id=kb_saved.id,
        filename=safe_filename,
        file_key=storage_key,
        file_size=file_size,
        file_type=file_type,
        status=VectorStatus.PROCESSING.value,
        chunk_count=0,
    )
    kb_saved.doc_count = (kb_saved.doc_count or 0) + 1

    # 必须先将 doc_entity 加入 session 并 flush/refresh，获取数据库生成的主键
    self.kb_repo.session.add(doc_entity)
    await self.kb_repo.session.flush()
    await self.kb_repo.session.refresh(doc_entity)
    log.info("[upload] doc_entity saved, id=%d", doc_entity.id)

    # 上传到 S3
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
    kb_saved.content_text = resume_text

    # 同步向量化
    try:
      await self._process_document(doc_entity, resume_text, kb_saved.id)
    except Exception as exc:
      # 设置失败状态（flush 使修改持久化），然后让异常继续传播
      # rollback 由 get_db 的异常处理统一执行，此处不重复 rollback
      doc_entity.status = VectorStatus.FAILED.value
      doc_entity.error_message = str(exc)[:1000]
      kb_saved.vector_status = VectorStatus.FAILED.value
      kb_saved.vector_error = str(exc)[:500]
      log.error("[upload] 向量化失败 kbId=%d docId=%d: %s", kb_saved.id, doc_entity.id, exc)
      await self.kb_repo.session.flush()
      raise

    await self.kb_repo.session.flush()
    await self.kb_repo.session.commit()
    return {
        "id": kb_saved.id,
        "duplicate": False,
        "name": kb_saved.name,
    }

  async def upload_document(
      self,
      kb_id: int,
      filename: str,
      content: bytes,
      file_type: str,
  ) -> KnowledgeDocumentEntity:
    """
    向已有知识库添加文档（多文档模式）。

    流程：
    1. 获取知识库
    2. 创建文档记录并 flush 以获取主键
    3. 上传到 S3
    4. 解析文本
    5. 同步向量化
    6. flush
    """
    kb_entity = await self.kb_repo.find_by_id(kb_id)
    if kb_entity is None:
      raise ValueError(f"知识库不存在: {kb_id}")
    if kb_entity.status != KnowledgeBaseStatus.ACTIVE.value:
      raise ValueError("知识库已停用")

    doc_uuid = str(uuid4())
    safe_filename = Path(filename).name
    storage_key = f"kb/{kb_id}/{doc_uuid}/{safe_filename}"

    doc_entity = KnowledgeDocumentEntity(
        doc_id=doc_uuid,
        kb_id=kb_id,
        filename=safe_filename,
        file_key=storage_key,
        file_size=len(content),
        file_type=file_type,
        status=VectorStatus.PROCESSING.value,
        chunk_count=0,
    )
    kb_entity.doc_count = (kb_entity.doc_count or 0) + 1

    # 必须先将 doc_entity 加入 session 并 flush/refresh，获取数据库生成的主键
    self.kb_repo.session.add(doc_entity)
    await self.kb_repo.session.flush()
    await self.kb_repo.session.refresh(doc_entity)
    log.info("[upload_document] doc_entity saved, id=%d", doc_entity.id)

    # 上传到 S3
    await upload_file(content, storage_key, self._content_type(file_type))
    log.info(
        "[upload_document] put_object success bucket=%s key=%s",
        _get_storage_bucket(),
        storage_key,
    )

    # 解析文本
    text = await parse_document(content, self._content_type(file_type), filename)

    # 同步向量化
    try:
      await self._process_document(doc_entity, text, kb_id)
    except Exception as exc:
      # 设置失败状态（flush 使修改持久化），然后让异常继续传播
      # rollback 由 get_db 的异常处理统一执行
      doc_entity.status = VectorStatus.FAILED.value
      doc_entity.error_message = str(exc)[:1000]
      log.error("[upload_document] 向量化失败 kbId=%d docId=%d: %s", kb_id, doc_entity.id, exc)
      await self.kb_repo.session.flush()
      raise

    await self.kb_repo.session.flush()
    return doc_entity

  async def _process_document(
      self,
      document: KnowledgeDocumentEntity,
      text: str,
      kb_id: int,
  ) -> None:
    """同步向量化文档。"""
    chunks = self._document_processor.split_into_chunks(
        text, self._chunk_size, self._chunk_overlap
    )
    if not chunks:
      raise ValueError("文档中没有可向量化的内容")

    embeddings = await self._embedding_client.embed_batch(chunks)
    if len(embeddings) != len(chunks):
      raise ValueError("Embedding 返回数量与文本块数量不一致")

    self.kb_repo.session.add_all([
        KnowledgeChunkEntity(
            doc_id=document.id,
            kb_id=kb_id,
            content=content,
            chunk_index=index,
            embedding=embeddings[index],
        )
        for index, content in enumerate(chunks)
    ])

    document.chunk_count = len(chunks)
    document.status = VectorStatus.COMPLETED.value
    document.error_message = None

    # 更新知识库的向量状态
    kb = await self.kb_repo.find_by_id(kb_id)
    if kb:
      kb.vector_status = VectorStatus.COMPLETED.value
      kb.chunk_count = (kb.chunk_count or 0) + len(chunks)

    await self.kb_repo.session.flush()

  async def delete_kb(self, kb_id: int) -> None:
    from sqlalchemy import delete
    kb_entity = await self.kb_repo.find_by_id_with_documents(kb_id)
    if kb_entity is None:
      return

    # 删除 RAG 会话中的知识库关联（必须先于 KB 删除，否则外键约束阻止）
    await self._remove_rag_session_associations(kb_id)

    # 删除关联文档的文件
    for doc in kb_entity.documents:
      try:
        await delete_file(doc.file_key)
      except Exception as e:
        log.warning("S3 delete failed for doc %s: %s", doc.doc_id, e)

    # 删除向量数据（chunks 通过 CASCADE 自动级联删除）
    try:
      await self.kb_repo.session.execute(
          delete(KnowledgeChunkEntity).where(KnowledgeChunkEntity.kb_id == kb_id)
      )
    except Exception as e:
      log.warning("Delete chunks failed for KB %d: %s", kb_id, e)

    # 删除知识库文件
    if kb_entity.storage_key:
      try:
        await delete_file(kb_entity.storage_key)
      except Exception as e:
        log.warning("S3 delete failed for KB %d: %s", kb_id, e)

    await self.kb_repo.delete(kb_id)

  async def _remove_rag_session_associations(self, kb_id: int) -> None:
    from sqlalchemy import select, update
    from app.models.knowledge_base import RagChatSessionEntity

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

  async def delete_document(self, kb_id: int, doc_id: str) -> None:
    """删除知识库下的单个文档。"""
    from sqlalchemy import delete
    kb_entity = await self.kb_repo.find_by_id_with_documents(kb_id)
    if kb_entity is None:
      return

    # 找到文档
    doc_to_delete = None
    for doc in kb_entity.documents:
      if doc.doc_id == doc_id:
        doc_to_delete = doc
        break

    if doc_to_delete:
      # 删除文件
      try:
        await delete_file(doc_to_delete.file_key)
      except Exception as e:
        log.warning("S3 delete failed for doc %s: %s", doc_id, e)

      # 删除 chunks
      await self.kb_repo.session.execute(
          delete(KnowledgeChunkEntity).where(KnowledgeChunkEntity.doc_id == doc_to_delete.id)
      )

      # 删除文档记录
      await self.kb_repo.session.delete(doc_to_delete)

      # 更新 doc_count
      kb_entity.doc_count = max((kb_entity.doc_count or 1) - 1, 0)
      await self.kb_repo.session.flush()

  async def reprocess_document(self, kb_id: int, doc_id: str) -> KnowledgeDocumentEntity:
    """重新处理文档。"""
    from sqlalchemy import delete, select
    kb_entity = await self.kb_repo.find_by_id_with_documents(kb_id)
    if kb_entity is None:
      raise ValueError(f"知识库不存在: {kb_id}")

    doc_entity = None
    for doc in kb_entity.documents:
      if doc.doc_id == doc_id:
        doc_entity = doc
        break

    if doc_entity is None:
      raise ValueError(f"文档不存在: {doc_id}")

    # 重置状态
    doc_entity.status = VectorStatus.PROCESSING.value
    doc_entity.error_message = None
    old_chunk_count = doc_entity.chunk_count or 0
    doc_entity.chunk_count = 0

    # 删除旧 chunks
    await self.kb_repo.session.execute(
        delete(KnowledgeChunkEntity).where(KnowledgeChunkEntity.doc_id == doc_entity.id)
    )

    # 下载文件
    log.info(
        "[reprocess] attempting get_object bucket=%s key=%s",
        _get_storage_bucket(),
        doc_entity.file_key,
    )
    try:
      from app.infrastructure.storage.file_storage import download_file
      file_data, _ = await download_file(doc_entity.file_key)
    except Exception as exc:
      log.error(
          "[reprocess] get_object failed bucket=%s key=%s error=%s",
          _get_storage_bucket(),
          doc_entity.file_key,
          exc,
      )
      raise ValueError(f"文件下载失败: {doc_entity.file_key}") from exc

    log.info(
        "[reprocess] get_object success bucket=%s key=%s size=%d",
        _get_storage_bucket(),
        doc_entity.file_key,
        len(file_data),
    )

    # 解析并向量化
    try:
      text = await parse_document(file_data, self._content_type(doc_entity.file_type), doc_entity.filename)
      await self._process_document(doc_entity, text, kb_id)
    except Exception as exc:
      doc_entity.status = VectorStatus.FAILED.value
      doc_entity.error_message = str(exc)[:1000]
      await self.kb_repo.session.flush()
      raise ValueError(f"文档处理失败: {str(exc)}") from exc

    # 更新知识库的 chunk_count
    kb_entity.chunk_count = max((kb_entity.chunk_count or 0) - old_chunk_count + doc_entity.chunk_count, 0)
    await self.kb_repo.session.flush()
    await self.kb_repo.session.refresh(doc_entity)
    return doc_entity

  async def list_documents(self, kb_id: int) -> list[KnowledgeDocumentEntity]:
    """列出知识库下的所有文档。"""
    kb_entity = await self.kb_repo.find_by_id_with_documents(kb_id)
    if kb_entity is None:
      raise ValueError(f"知识库不存在: {kb_id}")
    return kb_entity.documents

  async def get_document(self, kb_id: int, doc_id: str) -> KnowledgeDocumentEntity | None:
    """获取单个文档。"""
    kb_entity = await self.kb_repo.find_by_id_with_documents(kb_id)
    if kb_entity is None:
      return None
    for doc in kb_entity.documents:
      if doc.doc_id == doc_id:
        return doc
    return None

  @staticmethod
  def _content_type(file_type: str) -> str:
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "md": "text/markdown",
        "txt": "text/plain",
    }.get(file_type, "application/octet-stream")
