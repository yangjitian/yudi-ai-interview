import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.errors import BusinessException, ErrorCode
from app.core.result import ApiResponse
from app.models.kb_dto import (
    CreateSessionRequest,
    SendMessageRequest,
    SendMessageResponse,
    SessionDTO,
    SessionDetailDTO,
    SessionListItemDTO,
    UpdateTitleRequest,
    UpdateKnowledgeBasesRequest,
)
from app.repositories.kb_repository import KbRepository, RagChatRepository
from app.services.kb.chat import RagChatService
from app.services.kb.query import KnowledgeBaseQueryService


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/rag-chat", tags=["RAG 聊天"])


@router.post("/sessions", response_model=ApiResponse[SessionDTO])
async def create_session(
    req: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  rag_repo = RagChatRepository(db)
  query_svc = KnowledgeBaseQueryService(KbRepository(db))
  chat_svc = RagChatService(rag_repo, query_svc)
  result = await chat_svc.create_session(req.title, req.knowledge_base_ids)
  return ApiResponse.success(data=result)


@router.get("/sessions", response_model=ApiResponse[list[SessionListItemDTO]])
async def list_sessions(db: AsyncSession = Depends(get_db)) -> ApiResponse:
  rag_repo = RagChatRepository(db)
  query_svc = KnowledgeBaseQueryService(KbRepository(db))
  chat_svc = RagChatService(rag_repo, query_svc)
  result = await chat_svc.list_sessions()
  return ApiResponse.success(data=result)


@router.get("/sessions/{session_id}", response_model=ApiResponse[SessionDetailDTO])
async def get_session_detail(
    session_id: int = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  rag_repo = RagChatRepository(db)
  query_svc = KnowledgeBaseQueryService(KbRepository(db))
  chat_svc = RagChatService(rag_repo, query_svc)
  result = await chat_svc.get_session_detail(session_id)
  if result is None:
    raise BusinessException(ErrorCode.NOT_FOUND, "会话不存在")
  return ApiResponse.success(data=result)


@router.delete("/sessions/{session_id}", response_model=ApiResponse[None])
async def delete_session(
    session_id: int = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  rag_repo = RagChatRepository(db)
  query_svc = KnowledgeBaseQueryService(KbRepository(db))
  chat_svc = RagChatService(rag_repo, query_svc)
  await chat_svc.delete_session(session_id)
  return ApiResponse.success()


@router.put("/sessions/{session_id}/title", response_model=ApiResponse[None])
async def update_title(
    session_id: int = Path(description="会话ID"),
    req: UpdateTitleRequest = None,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  rag_repo = RagChatRepository(db)
  query_svc = KnowledgeBaseQueryService(KbRepository(db))
  chat_svc = RagChatService(rag_repo, query_svc)
  if req:
    await chat_svc.update_title(session_id, req.title)
  return ApiResponse.success()


@router.put("/sessions/{session_id}/pin", response_model=ApiResponse[None])
async def toggle_pin(
    session_id: int = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  rag_repo = RagChatRepository(db)
  query_svc = KnowledgeBaseQueryService(KbRepository(db))
  chat_svc = RagChatService(rag_repo, query_svc)
  await chat_svc.toggle_pin(session_id)
  return ApiResponse.success()


@router.put(
    "/sessions/{session_id}/knowledge-bases", response_model=ApiResponse[None]
)
async def update_knowledge_bases(
    req: UpdateKnowledgeBasesRequest,
    session_id: int = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  rag_repo = RagChatRepository(db)
  chat_svc = RagChatService(rag_repo, KnowledgeBaseQueryService(KbRepository(db)))
  await chat_svc.update_knowledge_bases(session_id, req.knowledge_base_ids)
  return ApiResponse.success()


@router.get(
    "/sessions/{session_id}/messages", response_model=ApiResponse[list[dict]]
)
async def list_messages(
    session_id: int = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  rag_repo = RagChatRepository(db)
  messages = await rag_repo.get_messages(session_id)
  return ApiResponse.success(data=[
      {
          "id": message.id,
          "role": message.type.lower(),
          "content": message.content,
          "created_at": message.created_at,
      }
      for message in messages
  ])


@router.post(
    "/sessions/{session_id}/messages",
    response_model=ApiResponse[SendMessageResponse],
)
async def send_message(
    req: SendMessageRequest,
    session_id: int = Path(description="会话ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  rag_repo = RagChatRepository(db)
  chat_svc = RagChatService(rag_repo, KnowledgeBaseQueryService(KbRepository(db)))
  result = await chat_svc.send_message(session_id, req.question)
  return ApiResponse.success(data=result)


@router.post("/sessions/{session_id}/messages/stream")
async def send_message_stream(
    session_id: int = Path(description="会话ID"),
    req: SendMessageRequest = None,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
  rag_repo = RagChatRepository(db)
  query_svc = KnowledgeBaseQueryService(KbRepository(db))
  chat_svc = RagChatService(rag_repo, query_svc)

  if req is None:
    raise BusinessException(ErrorCode.BAD_REQUEST, "请求体不能为空")

  # 获取历史消息上下文（与 Java 版本一致，最多取最近 10 条）
  history = []
  if req.include_history:
    messages = await rag_repo.get_messages(session_id)
    for msg in messages[-20:]:  # 取最近 20 条消息
        history.append({
            "role": msg.type.lower(),
            "content": msg.content,
        })

  message_id = await chat_svc.prepare_stream_message(session_id, req.question)

  async def event_generator() -> AsyncGenerator[str, None]:
    try:
      full_content = ""
      async for chunk in await chat_svc.get_stream_answer(session_id, req.question, history):
        full_content += chunk
        escaped = chunk.replace("\n", "\\n").replace("\r", "\\r")
        yield f"data: {escaped}\n\n"
      await chat_svc.complete_stream_message(message_id, full_content)
    except Exception as e:
      log.error("Stream error: sessionId=%d error=%s", session_id, e)
      await chat_svc.complete_stream_message(message_id, f"【错误】{str(e)}")
      yield f"data: 【错误】{str(e)}\n\n"

  return StreamingResponse(
      event_generator(),
      media_type="text/event-stream",
      headers={
          "Cache-Control": "no-cache",
          "Connection": "keep-alive",
          "X-Accel-Buffering": "no",
      },
  )
