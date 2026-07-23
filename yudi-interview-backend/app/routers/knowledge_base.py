import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Path as ApiPath, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.errors import BusinessException, ErrorCode
from app.core.result import ApiResponse
from app.models.knowledge_base import VectorStatus
from app.models.kb_dto import KnowledgeBaseListItemDTO, KnowledgeDocumentDTO, QueryRequest, QueryResponse
from app.repositories.kb_repository import KbRepository, RagChatRepository
from app.services.kb.query import KnowledgeBaseQueryService
from app.services.kb.upload import KnowledgeBaseUploadService


log = logging.getLogger(__name__)

# 主路由：/api/knowledge-base（与 Java 版本一致）
router = APIRouter(prefix="/api/knowledge-base", tags=["知识库"])


def _to_document_dto(entity, kb_id: int) -> KnowledgeDocumentDTO:
  return KnowledgeDocumentDTO(
      docId=entity.doc_id,
      kbId=str(kb_id),
      filename=entity.filename,
      fileSize=entity.file_size,
      fileType=entity.file_type,
      chunkCount=entity.chunk_count or 0,
      status=entity.status,
      errorMessage=entity.error_message,
      createdAt=entity.created_at,
      updatedAt=entity.updated_at,
  )


# ========== 上传下载 API ==========

@router.post("/upload", response_model=ApiResponse[dict])
async def upload_document(
    file: Annotated[UploadFile, File(description="文档文件")],
    name: Annotated[str, Form(description="文档名称")],
    category: Annotated[str | None, Form(description="分类")] = None,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  content = await file.read()
  if not content:
    raise BusinessException(ErrorCode.BAD_REQUEST, "文件内容为空")

  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  result = await upload_svc.upload(
      content=content,
      filename=file.filename or "unknown",
      name=name,
      content_type=file.content_type,
      category=category,
  )
  return ApiResponse.success(data=result)


@router.get("/{kb_id}/download")
async def download_document(
    kb_id: int = ApiPath(description="知识库ID"),
    db: AsyncSession = Depends(get_db),
):
  from fastapi.responses import StreamingResponse
  kb_repo = KbRepository(db)
  entity = await kb_repo.find_by_id(kb_id)
  if entity is None:
    raise BusinessException(ErrorCode.NOT_FOUND, "知识库不存在")
  if not entity.storage_key:
    raise BusinessException(ErrorCode.NOT_FOUND, "文件不存在")
  from app.infrastructure.storage.file_storage import download_file
  file_bytes, content_type = await download_file(entity.storage_key)
  return StreamingResponse(
      iter([file_bytes]),
      media_type=content_type or "application/octet-stream",
      headers={"Content-Disposition": f"attachment; filename*=UTF-8''{entity.original_filename}"},
  )


# ========== 列表与详情 API ==========

@router.get("", response_model=ApiResponse[list[KnowledgeBaseListItemDTO]])
async def list_documents(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  kb_repo = KbRepository(db)
  entities, total = await kb_repo.list_all(page, page_size)
  items = [
      KnowledgeBaseListItemDTO(
          id=e.id,
          name=e.name,
          category=e.category,
          original_filename=e.original_filename,
          file_size=e.file_size,
          uploaded_at=e.uploaded_at,
          access_count=e.access_count or 0,
          question_count=e.question_count or 0,
          vector_status=e.vector_status,
          chunk_count=e.chunk_count,
          vector_error=e.vector_error,
      )
      for e in entities
  ]
  return ApiResponse.success(data=items)


@router.get("/list", response_model=ApiResponse[list[KnowledgeBaseListItemDTO]])
async def list_documents_alias(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  return await list_documents(page=page, page_size=page_size, db=db)


@router.delete("/{kb_id}", response_model=ApiResponse[None])
async def delete_document(
    kb_id: int = ApiPath(description="知识库ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  await upload_svc.delete_kb(kb_id)
  return ApiResponse.success()


# ========== 多文档 API（新增，与 knowledge.py 合并）==========

@router.post(
    "/{kb_id}/documents",
    response_model=ApiResponse[KnowledgeDocumentDTO],
)
async def upload_kb_document(
    kb_id: int = ApiPath(description="知识库ID"),
    file: UploadFile = File(description="PDF、DOC、DOCX、TXT、MD 文档"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  filename = file.filename or "unknown"
  file_type = Path(filename).suffix.lower().lstrip(".")
  if file_type not in {"pdf", "docx", "doc", "txt", "md"}:
    raise BusinessException(ErrorCode.BAD_REQUEST, "仅支持 PDF、DOC、DOCX、TXT、MD 文件")
  content = await file.read()
  if not content:
    raise BusinessException(ErrorCode.BAD_REQUEST, "文件内容不能为空")
  if len(content) > 50 * 1024 * 1024:
    raise BusinessException(ErrorCode.BAD_REQUEST, "文件大小不能超过 50MB")

  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  entity = await upload_svc.upload_document(kb_id, filename, content, file_type)
  return ApiResponse.success(data=_to_document_dto(entity, kb_id))


@router.get(
    "/{kb_id}/documents",
    response_model=ApiResponse[list[KnowledgeDocumentDTO]],
)
async def list_kb_documents(
    kb_id: int = ApiPath(description="知识库ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  entities = await upload_svc.list_documents(kb_id)
  return ApiResponse.success(data=[_to_document_dto(e, kb_id) for e in entities])


@router.delete(
    "/{kb_id}/documents/{doc_id}", response_model=ApiResponse[None]
)
async def delete_kb_document(
    kb_id: int = ApiPath(description="知识库ID"),
    doc_id: str = ApiPath(description="文档业务 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  await upload_svc.delete_document(kb_id, doc_id)
  return ApiResponse.success()


@router.post(
    "/{kb_id}/documents/{doc_id}/reprocess",
    response_model=ApiResponse[KnowledgeDocumentDTO],
)
async def reprocess_kb_document(
    kb_id: int = ApiPath(description="知识库ID"),
    doc_id: str = ApiPath(description="文档业务 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  entity = await upload_svc.reprocess_document(kb_id, doc_id)
  return ApiResponse.success(data=_to_document_dto(entity, kb_id))


# ========== 查询 API ==========

@router.post("/query", response_model=ApiResponse[QueryResponse])
async def query_knowledge_base(
    req: QueryRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  query_svc = KnowledgeBaseQueryService(KbRepository(db))
  result = await query_svc.query(
      query_text=req.query_text,
      knowledge_base_ids=req.knowledge_base_ids,
      top_k=req.top_k,
  )
  return ApiResponse.success(data=result)


# ========== 分类管理 API ==========

@router.get("/categories", response_model=ApiResponse[list[str]])
async def list_categories(db: AsyncSession = Depends(get_db)) -> ApiResponse:
  kb_repo = KbRepository(db)
  entities, _ = await kb_repo.list_all(1, 1000)
  categories = sorted({e.category for e in entities if e.category})
  return ApiResponse.success(data=categories)


@router.get("/category/{category}", response_model=ApiResponse[list[KnowledgeBaseListItemDTO]])
async def get_by_category(
    category: str,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  kb_repo = KbRepository(db)
  entities, _ = await kb_repo.list_all(1, 1000)
  filtered = [e for e in entities if e.category == category]
  items = [
      KnowledgeBaseListItemDTO(
          id=e.id,
          name=e.name,
          category=e.category,
          original_filename=e.original_filename,
          file_size=e.file_size or 0,
          uploaded_at=e.uploaded_at,
          access_count=e.access_count or 0,
          question_count=e.question_count or 0,
          vector_status=e.vector_status,
          chunk_count=e.chunk_count,
          vector_error=e.vector_error,
      )
      for e in filtered
  ]
  return ApiResponse.success(data=items)


@router.get("/uncategorized", response_model=ApiResponse[list[KnowledgeBaseListItemDTO]])
async def get_uncategorized(db: AsyncSession = Depends(get_db)) -> ApiResponse:
  kb_repo = KbRepository(db)
  entities, _ = await kb_repo.list_all(1, 1000)
  filtered = [e for e in entities if not e.category]
  items = [
      KnowledgeBaseListItemDTO(
          id=e.id,
          name=e.name,
          category=e.category,
          original_filename=e.original_filename,
          file_size=e.file_size or 0,
          uploaded_at=e.uploaded_at,
          access_count=e.access_count or 0,
          question_count=e.question_count or 0,
          vector_status=e.vector_status,
          chunk_count=e.chunk_count,
          vector_error=e.vector_error,
      )
      for e in filtered
  ]
  return ApiResponse.success(data=items)


@router.put("/{kb_id}/category", response_model=ApiResponse[None])
async def update_category(
    kb_id: int = ApiPath(description="知识库ID"),
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  kb_repo = KbRepository(db)
  entity = await kb_repo.find_by_id(kb_id)
  if entity is None:
    raise BusinessException(ErrorCode.NOT_FOUND, "知识库不存在")
  entity.category = category
  await db.flush()
  return ApiResponse.success()


# ========== 搜索与统计 API ==========

@router.get("/search", response_model=ApiResponse[list[KnowledgeBaseListItemDTO]])
async def search_documents(
    keyword: str = Query(description="搜索关键词"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  kb_repo = KbRepository(db)
  entities, _ = await kb_repo.list_all(1, 1000)
  kw = keyword.lower()
  filtered = [
      e for e in entities
      if kw in (e.name or "").lower() or kw in (e.original_filename or "").lower()
  ]
  items = [
      KnowledgeBaseListItemDTO(
          id=e.id,
          name=e.name,
          category=e.category,
          original_filename=e.original_filename,
          file_size=e.file_size or 0,
          uploaded_at=e.uploaded_at,
          access_count=e.access_count or 0,
          question_count=e.question_count or 0,
          vector_status=e.vector_status,
          chunk_count=e.chunk_count,
          vector_error=e.vector_error,
      )
      for e in filtered
  ]
  return ApiResponse.success(data=items)


@router.get("/stats", response_model=ApiResponse[dict])
async def get_stats(db: AsyncSession = Depends(get_db)) -> ApiResponse:
  kb_repo = KbRepository(db)
  rag_repo = RagChatRepository(db)

  # 与 Java 版本一致：
  # totalCount = 知识库总数
  # totalQuestionCount = rag_chat_messages 表中 USER 类型的消息数
  # totalAccessCount = 所有知识库的 access_count 聚合
  total_count = await kb_repo.count()
  total_question_count = await rag_repo.count_by_type("USER")
  total_access_count = await kb_repo.sum_access_count()
  completed_count = await kb_repo.count_by_vector_status(VectorStatus.COMPLETED.value)
  processing_count = await kb_repo.count_by_vector_status(VectorStatus.PROCESSING.value)

  log.info(
      "[stats] returning: totalCount=%d, totalQuestionCount=%d, totalAccessCount=%d, completedCount=%d, processingCount=%d",
      total_count, total_question_count, total_access_count, completed_count, processing_count,
  )

  return ApiResponse.success(data={
      "totalCount": total_count,
      "totalQuestionCount": total_question_count,
      "totalAccessCount": total_access_count,
      "completedCount": completed_count,
      "processingCount": processing_count,
  })


@router.get("/{kb_id}", response_model=ApiResponse[KnowledgeBaseListItemDTO])
async def get_document(
    kb_id: int = ApiPath(description="知识库ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  kb_repo = KbRepository(db)
  entity = await kb_repo.find_by_id(kb_id)
  if entity is None:
    raise BusinessException(ErrorCode.NOT_FOUND, "知识库不存在")
  return ApiResponse.success(data=KnowledgeBaseListItemDTO(
      id=entity.id,
      name=entity.name,
      category=entity.category,
      original_filename=entity.original_filename,
      file_size=entity.file_size or 0,
      uploaded_at=entity.uploaded_at,
      access_count=entity.access_count or 0,
      question_count=entity.question_count or 0,
      vector_status=entity.vector_status,
      chunk_count=entity.chunk_count,
      vector_error=entity.vector_error,
  ))


@router.post("/{kb_id}/revectorize", response_model=ApiResponse[None])
async def revectorize_document(
    kb_id: int = ApiPath(description="知识库ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  kb_repo = KbRepository(db)
  entity = await kb_repo.find_by_id(kb_id)
  if entity is None:
    raise BusinessException(ErrorCode.NOT_FOUND, "知识库不存在")
  if entity.content_text:
    from app.infrastructure.redis.vectorize_producer import send_vectorize_task
    await send_vectorize_task(kb_id, entity.content_text)
  return ApiResponse.success()


# ========== 路由别名：从 /api/knowledge/* 重定向到 /api/knowledge-base/* ==========

_alias_router = APIRouter(prefix="/api/knowledge", tags=["知识库（兼容别名）"])


@_alias_router.get("/bases", response_model=ApiResponse[list[dict]])
async def alias_list_bases(
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  """别名：/api/knowledge/bases -> /api/knowledge-base"""
  kb_repo = KbRepository(db)
  entities, _ = await kb_repo.list_all(1, 1000)
  return ApiResponse.success(data=[
      {
          "id": e.id,
          "name": e.name,
          "category": e.category,
          "original_filename": e.original_filename,
          "file_size": e.file_size,
          "uploaded_at": e.uploaded_at,
          "access_count": e.access_count or 0,
          "question_count": e.question_count or 0,
          "vector_status": e.vector_status,
          "chunk_count": e.chunk_count,
          "vector_error": e.vector_error,
      }
      for e in entities
  ])


@_alias_router.get("/bases/{kb_id}", response_model=ApiResponse[dict])
async def alias_get_base(
    kb_id: str = ApiPath(description="知识库ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  """别名：/api/knowledge/bases/{kb_id} -> /api/knowledge-base/{id}"""
  try:
    kb_int_id = int(kb_id)
  except ValueError:
    raise BusinessException(ErrorCode.BAD_REQUEST, "无效的知识库ID")

  kb_repo = KbRepository(db)
  entity = await kb_repo.find_by_id(kb_int_id)
  if entity is None:
    raise BusinessException(ErrorCode.NOT_FOUND, "知识库不存在")
  return ApiResponse.success(data={
      "id": entity.id,
      "name": entity.name,
      "category": entity.category,
      "original_filename": entity.original_filename,
      "file_size": entity.file_size,
      "uploaded_at": entity.uploaded_at,
      "access_count": entity.access_count or 0,
      "question_count": entity.question_count or 0,
      "vector_status": entity.vector_status,
      "chunk_count": entity.chunk_count,
      "vector_error": entity.vector_error,
  })


@_alias_router.post("/bases/{kb_id}/documents", response_model=ApiResponse[KnowledgeDocumentDTO])
async def alias_upload_document(
    kb_id: str = ApiPath(description="知识库业务ID"),
    file: UploadFile = File(description="文档"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  """别名：/api/knowledge/bases/{kb_id}/documents -> /api/knowledge-base/{id}/documents"""
  try:
    kb_int_id = int(kb_id)
  except ValueError:
    raise BusinessException(ErrorCode.BAD_REQUEST, "无效的知识库ID")

  filename = file.filename or "unknown"
  file_type = Path(filename).suffix.lower().lstrip(".")
  if file_type not in {"pdf", "docx", "doc", "txt", "md"}:
    raise BusinessException(ErrorCode.BAD_REQUEST, "仅支持 PDF、DOC、DOCX、TXT、MD 文件")
  content = await file.read()
  if not content:
    raise BusinessException(ErrorCode.BAD_REQUEST, "文件内容不能为空")
  if len(content) > 50 * 1024 * 1024:
    raise BusinessException(ErrorCode.BAD_REQUEST, "文件大小不能超过 50MB")

  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  entity = await upload_svc.upload_document(kb_int_id, filename, content, file_type)
  return ApiResponse.success(data=_to_document_dto(entity, kb_int_id))


@_alias_router.get("/bases/{kb_id}/documents", response_model=ApiResponse[list[KnowledgeDocumentDTO]])
async def alias_list_documents(
    kb_id: str = ApiPath(description="知识库业务ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  """别名：/api/knowledge/bases/{kb_id}/documents -> /api/knowledge-base/{id}/documents"""
  try:
    kb_int_id = int(kb_id)
  except ValueError:
    raise BusinessException(ErrorCode.BAD_REQUEST, "无效的知识库ID")

  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  entities = await upload_svc.list_documents(kb_int_id)
  return ApiResponse.success(data=[_to_document_dto(e, kb_int_id) for e in entities])


@_alias_router.delete("/bases/{kb_id}/documents/{doc_id}", response_model=ApiResponse[None])
async def alias_delete_document(
    kb_id: str = ApiPath(description="知识库业务ID"),
    doc_id: str = ApiPath(description="文档业务 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  """别名：/api/knowledge/bases/{kb_id}/documents/{doc_id} -> /api/knowledge-base/{id}/documents/{doc_id}"""
  try:
    kb_int_id = int(kb_id)
  except ValueError:
    raise BusinessException(ErrorCode.BAD_REQUEST, "无效的知识库ID")

  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  await upload_svc.delete_document(kb_int_id, doc_id)
  return ApiResponse.success()


@_alias_router.post("/bases/{kb_id}/documents/{doc_id}/reprocess", response_model=ApiResponse[KnowledgeDocumentDTO])
async def alias_reprocess_document(
    kb_id: str = ApiPath(description="知识库业务ID"),
    doc_id: str = ApiPath(description="文档业务 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  """别名：/api/knowledge/bases/{kb_id}/documents/{doc_id}/reprocess -> /api/knowledge-base/{id}/documents/{doc_id}/reprocess"""
  try:
    kb_int_id = int(kb_id)
  except ValueError:
    raise BusinessException(ErrorCode.BAD_REQUEST, "无效的知识库ID")

  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  entity = await upload_svc.reprocess_document(kb_int_id, doc_id)
  return ApiResponse.success(data=_to_document_dto(entity, kb_int_id))
