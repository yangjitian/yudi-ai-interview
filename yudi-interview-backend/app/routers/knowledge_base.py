import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Path as ApiPath,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.constants import KB_MAX_UPLOAD_BYTES
from app.core.errors import (
    BusinessException,
    ErrorCode,
    RateLimitExceededException,
)
from app.core.result import ApiResponse
from app.core.upload_validation import (
    KB_EXTENSION_CONTENT_TYPES,
    check_upload_content_length,
    read_upload_with_limit,
    validate_upload_metadata,
)
from app.infrastructure.redis.question_generation import (
    QuestionGenStreamProducer,
    cancel_running_question_generation,
)
from app.infrastructure.redis.session_cache import SessionCache
from app.middleware.rate_limit import RateLimitConfig, check_rate_limit
from app.models.knowledge_base import VectorStatus
from app.models.kb_dto import (
    CreateKnowledgeBaseInterviewRequest,
    CreateKnowledgeBaseQuestionRequest,
    KnowledgeBaseInterviewCapacityResponse,
    KnowledgeBaseInterviewQuestionResponse,
    KnowledgeBaseInterviewSessionResponse,
    KnowledgeBaseListItemDTO,
    KnowledgeBaseQuestionCategoryCount,
    KnowledgeBaseQuestionDTO,
    KnowledgeBaseQuestionStatus,
    QuestionGenerationConfig,
    QuestionGenerationStatusResponse,
    QueryRequest,
    QueryResponse,
    UpdateKnowledgeBaseQuestionRequest,
    UpdateKnowledgeBaseQuestionStatusRequest,
)
from app.repositories.kb_repository import (
    KbRepository,
    KnowledgeBaseQuestionRepository,
    RagChatRepository,
)
from app.repositories.interview_repository import InterviewAnswerRepository, InterviewRepository
from app.services.interview.session_service import InterviewSessionService
from app.services.kb.interview import KnowledgeBaseInterviewService
from app.services.kb.query import KnowledgeBaseQueryService
from app.services.kb.question_generation_state import QuestionGenerationStateService
from app.services.kb.questions import KnowledgeBaseQuestionService
from app.services.kb.upload import KnowledgeBaseUploadService


log = logging.getLogger(__name__)

# 主路由：/api/knowledge-base（与 Java 版本一致）
router = APIRouter(prefix="/api/knowledge-base", tags=["知识库"])
_interview_router = APIRouter(
    prefix="/api/knowledge-base-interviews",
    tags=["知识库面试"],
)
_question_generation_router = APIRouter(
    prefix="/api/knowledgebase",
    tags=["知识库题目生成"],
)

_QUESTION_GENERATION_RATE_LIMIT_KEY = (
    "{KnowledgeBaseInterviewController:generateQuestions}"
)
_QUESTION_GENERATION_RATE_LIMITS = (
    RateLimitConfig(count=2, interval_seconds=1, dimension="GLOBAL"),
    RateLimitConfig(count=2, interval_seconds=1, dimension="IP"),
)


class GenerateKnowledgeBaseQuestionsRequest(BaseModel):
  difficulty: str | None = Field(
      default=None,
      pattern="^(junior|mid|senior)$",
  )
  questionCount: int = Field(ge=1, le=30)
  followUpCount: int | None = Field(default=None, ge=0, le=5)
  categoryLimit: int = Field(ge=1, le=5)
  llmProvider: str | None = Field(default=None, max_length=64)


def _empty_string_to_none(value: str | None) -> str | None:
  return None if value == "" else value


def _question_service(db: AsyncSession) -> KnowledgeBaseQuestionService:
  return KnowledgeBaseQuestionService(
      KbRepository(db),
      KnowledgeBaseQuestionRepository(db),
  )


def _question_generation_state_service(
    db: AsyncSession = Depends(get_db),
) -> QuestionGenerationStateService:
  return QuestionGenerationStateService(
      KbRepository(db),
      KnowledgeBaseQuestionRepository(db),
  )


def _question_generation_producer() -> QuestionGenStreamProducer:
  return QuestionGenStreamProducer()


async def _check_question_generation_rate_limit(request: Request) -> None:
  for config in _QUESTION_GENERATION_RATE_LIMITS:
    allowed, _remaining = await check_rate_limit(
        request,
        _QUESTION_GENERATION_RATE_LIMIT_KEY,
        config,
    )
    if not allowed:
      raise RateLimitExceededException()


def _knowledge_base_interview_service(
    db: AsyncSession,
) -> KnowledgeBaseInterviewService:
  return KnowledgeBaseInterviewService(
      KbRepository(db),
      KnowledgeBaseQuestionRepository(db),
      InterviewSessionService(
          session_repo=InterviewRepository(db),
          answer_repo=InterviewAnswerRepository(db),
          session_cache=SessionCache(),
      ),
  )


def _to_knowledge_base_interview_response(
    session,
) -> KnowledgeBaseInterviewSessionResponse:
  return KnowledgeBaseInterviewSessionResponse(
      sessionId=session.session_id,
      resumeText=session.resume_text,
      totalQuestions=session.total_questions,
      currentQuestionIndex=session.current_index,
      questions=[
          KnowledgeBaseInterviewQuestionResponse(
              questionIndex=question.question_index,
              question=question.question,
              type=question.type,
              category=question.category,
              topicSummary=question.topic_summary,
              userAnswer=question.answer,
              score=question.score,
              feedback=question.feedback,
              isFollowUp=question.is_follow_up,
              parentQuestionIndex=question.parent_question_index,
              referenceAnswer=question.reference_answer,
              keyPoints=question.key_points,
              scoringRubric=question.scoring_rubric,
              sourceContext=question.source_context,
          )
          for question in session.questions
      ],
      status=session.status,
      knowledgeBaseId=session.knowledge_base_id,
      interviewCategory=session.interview_category,
  )


@_question_generation_router.post(
    "/{kb_id}/questions/generate",
    response_model=ApiResponse[QuestionGenerationStatusResponse],
    dependencies=[Depends(_check_question_generation_rate_limit)],
)
async def generate_knowledge_base_questions(
    request: GenerateKnowledgeBaseQuestionsRequest,
    kb_id: int = ApiPath(description="知识库 ID"),
    state_service: QuestionGenerationStateService = Depends(
        _question_generation_state_service
    ),
    producer: QuestionGenStreamProducer = Depends(
        _question_generation_producer
    ),
) -> ApiResponse:
  config = QuestionGenerationConfig(
      difficulty=request.difficulty or "mid",
      questionCount=request.questionCount,
      followUpCount=(
          request.followUpCount if request.followUpCount is not None else 2
      ),
      categoryLimit=request.categoryLimit,
      llmProvider=(
          request.llmProvider.strip()
          if request.llmProvider and request.llmProvider.strip()
          else None
      ),
  )
  task_id = await state_service.submit(kb_id, config)
  response = await state_service.get_status(kb_id)
  sent = await producer.send_generate_task(kb_id, task_id, 0)
  if not sent:
    state_service.session.expire_all()
    response = await state_service.get_status(kb_id)
  return ApiResponse.success(data=response)


@_question_generation_router.get(
    "/{kb_id}/questions/generation-status",
    response_model=ApiResponse[QuestionGenerationStatusResponse],
)
async def get_question_generation_status(
    kb_id: int = ApiPath(description="知识库 ID"),
    state_service: QuestionGenerationStateService = Depends(
        _question_generation_state_service
    ),
) -> ApiResponse:
  return ApiResponse.success(data=await state_service.get_status(kb_id))


@_question_generation_router.post(
    "/{kb_id}/questions/generation/cancel",
    response_model=ApiResponse[QuestionGenerationStatusResponse],
)
async def cancel_question_generation(
    kb_id: int = ApiPath(description="知识库 ID"),
    state_service: QuestionGenerationStateService = Depends(
        _question_generation_state_service
    ),
) -> ApiResponse:
  response = await state_service.cancel(kb_id)
  running_task_interrupted = False
  if (
      response.questionGenStatus.value == "CANCELLED"
      and response.questionGenTaskId is not None
  ):
    running_task_interrupted = cancel_running_question_generation(
        response.questionGenTaskId
    )
  log.info(
      "题目生成取消请求已处理: kbId=%d taskId=%s status=%s "
      "savedCount=%d runningTaskInterrupted=%s",
      kb_id,
      response.questionGenTaskId,
      response.questionGenStatus.value,
      response.savedCount,
      running_task_interrupted,
  )
  return ApiResponse.success(data=response)


@_interview_router.post(
    "/sessions",
    response_model=ApiResponse[KnowledgeBaseInterviewSessionResponse],
)
async def create_knowledge_base_interview_session(
    request: CreateKnowledgeBaseInterviewRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  session = await _knowledge_base_interview_service(db).create_session(request)
  return ApiResponse.success(data=_to_knowledge_base_interview_response(session))


# ========== 题库手工管理 API ==========

@router.get(
    "/{kb_id}/questions",
    response_model=ApiResponse[list[KnowledgeBaseQuestionDTO]],
)
async def list_questions(
    kb_id: int = ApiPath(description="知识库 ID"),
    status: Annotated[
        KnowledgeBaseQuestionStatus | None,
        BeforeValidator(_empty_string_to_none),
        Query(),
    ] = None,
    category: str | None = Query(default=None),
    difficulty: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _question_service(db).list_questions(
      kb_id,
      status,
      category,
      difficulty,
      keyword,
  )
  return ApiResponse.success(data=result)


@router.get(
    "/{kb_id}/questions/categories",
    response_model=ApiResponse[list[KnowledgeBaseQuestionCategoryCount]],
)
async def list_question_categories(
    kb_id: int = ApiPath(description="知识库 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _question_service(db).list_categories(kb_id)
  return ApiResponse.success(data=result)


@router.get(
    "/{kb_id}/interview-capacity",
    response_model=ApiResponse[KnowledgeBaseInterviewCapacityResponse],
)
async def get_interview_capacity(
    kb_id: int = ApiPath(description="知识库 ID"),
    category: str | None = Query(default=None),
    difficulty: str = Query(default="mid"),
    main_question_count: int = Query(
        default=5,
        alias="mainQuestionCount",
        ge=1,
        le=20,
    ),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _knowledge_base_interview_service(db).get_capacity(
      kb_id,
      category,
      difficulty,
      main_question_count,
  )
  return ApiResponse.success(data=result)

@router.post(
    "/{kb_id}/questions",
    response_model=ApiResponse[KnowledgeBaseQuestionDTO],
)
async def create_question(
    request: CreateKnowledgeBaseQuestionRequest,
    kb_id: int = ApiPath(description="知识库 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _question_service(db).create_question(kb_id, request)
  return ApiResponse.success(data=result)


@router.put(
    "/questions/{question_id}",
    response_model=ApiResponse[KnowledgeBaseQuestionDTO],
)
async def update_question(
    request: UpdateKnowledgeBaseQuestionRequest,
    question_id: int = ApiPath(description="题目 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _question_service(db).update_question(question_id, request)
  return ApiResponse.success(data=result)


@router.put(
    "/questions/{question_id}/status",
    response_model=ApiResponse[KnowledgeBaseQuestionDTO],
)
async def update_question_status(
    request: UpdateKnowledgeBaseQuestionStatusRequest,
    question_id: int = ApiPath(description="题目 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await _question_service(db).update_status(question_id, request.status)
  return ApiResponse.success(data=result)


@router.delete(
    "/questions/{question_id}",
    response_model=ApiResponse[None],
)
async def delete_question(
    question_id: int = ApiPath(description="题目 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  await _question_service(db).delete_question(question_id)
  return ApiResponse.success()


# ========== 上传下载 API ==========

@router.post("/upload", response_model=ApiResponse[dict])
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File(description="文档文件")],
    name: Annotated[str, Form(description="文档名称")],
    category: Annotated[str | None, Form(description="分类")] = None,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  check_upload_content_length(
      request,
      KB_MAX_UPLOAD_BYTES,
      ErrorCode.FILE_TOO_LARGE,
  )
  filename = file.filename or "unknown"
  validate_upload_metadata(
      filename,
      file.content_type,
      KB_EXTENSION_CONTENT_TYPES,
      ErrorCode.KNOWLEDGE_BASE_FILE_TYPE_NOT_SUPPORTED,
  )
  content = await read_upload_with_limit(
      file,
      KB_MAX_UPLOAD_BYTES,
      ErrorCode.FILE_TOO_LARGE,
  )
  if not content:
    raise BusinessException(ErrorCode.BAD_REQUEST, "文件内容为空")

  upload_svc = KnowledgeBaseUploadService(KbRepository(db))
  result = await upload_svc.upload(
      content=content,
      filename=filename,
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
  # content_text 列不存在于数据库，需从 S3 重新下载并解析
  if not entity.storage_key:
    raise BusinessException(ErrorCode.BAD_REQUEST, "知识库无存储文件，无法重新向量化")
  from app.infrastructure.storage.file_storage import download_file
  from app.infrastructure.parser.document_parser import parse_document
  from app.infrastructure.redis.vectorize_producer import send_vectorize_task
  file_data, _ = await download_file(entity.storage_key)
  text = await parse_document(file_data, entity.content_type, entity.original_filename)
  await send_vectorize_task(kb_id, text)
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
