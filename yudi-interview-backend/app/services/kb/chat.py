import logging
from datetime import datetime

from sqlalchemy import select

from app.core.errors import BusinessException, ErrorCode
from app.models.knowledge_base import KnowledgeBaseEntity, RagChatMessageEntity, RagChatSessionEntity
from app.repositories.kb_repository import RagChatRepository
from app.services.kb.query import KnowledgeBaseQueryService
from app.utils.timezone_utils import get_beijing_now

log = logging.getLogger(__name__)


class RagChatService:
  def __init__(self, rag_repo: RagChatRepository, query_service: KnowledgeBaseQueryService):
    self.rag_repo = rag_repo
    self.query_service = query_service

  def _generate_title(self, knowledge_bases: list[KnowledgeBaseEntity]) -> str:
    """根据知识库生成会话标题，与 Java 版本保持一致"""
    if not knowledge_bases:
      return "新会话"
    if len(knowledge_bases) == 1:
      return knowledge_bases[0].name
    return f"{len(knowledge_bases)} 个知识库对话"

  async def create_session(
      self, title: str | None = None, knowledge_base_ids: list[int] | None = None
  ) -> dict:
    # 如果没有提供标题，根据知识库生成标题（与 Java 版本一致）
    if not title and knowledge_base_ids:
      result = await self.rag_repo.session.execute(
        select(KnowledgeBaseEntity).where(KnowledgeBaseEntity.id.in_(knowledge_base_ids))
      )
      kb_list = list(result.scalars().all())
      title = self._generate_title(kb_list)

    entity = RagChatSessionEntity(
        title=title or "新会话",
        status="ACTIVE",
        message_count=0,
        is_pinned=False,
    )
    saved = await self.rag_repo.save_session(entity)
    await self.rag_repo.replace_session_knowledge_bases(
        saved.id, knowledge_base_ids or []
    )
    return {"session_id": saved.id, "title": saved.title}

  async def list_sessions(self) -> list[dict]:
    sessions = await self.rag_repo.list_sessions()
    return [
        {
            "id": session.id,
            "title": session.title,
            "is_pinned": bool(session.is_pinned),
            "message_count": session.message_count or len(session.messages),
            "knowledge_base_names": [kb.name for kb in session.knowledge_bases],
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }
        for session in sessions
    ]

  async def get_session_detail(self, session_id: int) -> dict | None:
    session = await self.rag_repo.find_session_by_id(session_id)
    if session is None:
      return None
    messages = await self.rag_repo.get_messages(session_id)
    return {
        "id": session.id,
        "title": session.title,
        "is_pinned": bool(session.is_pinned),
        "knowledge_bases": [
            {
                "id": kb.id,
                "name": kb.name,
                "category": kb.category,
                "vector_status": kb.vector_status,
                "chunk_count": kb.chunk_count,
            }
            for kb in session.knowledge_bases
        ],
        "messages": [
            {
                "id": message.id,
                "role": message.type.lower(),
                "content": message.content,
                "created_at": message.created_at,
            }
            for message in messages
        ],
        "created_at": session.created_at,
    }

  async def delete_session(self, session_id: int) -> None:
    await self.rag_repo.delete_session(session_id)

  async def update_title(self, session_id: int, title: str) -> None:
    await self.rag_repo.update_session_title(session_id, title)

  async def update_knowledge_bases(
      self, session_id: int, knowledge_base_ids: list[int]
  ) -> None:
    session = await self.rag_repo.find_session_by_id(session_id)
    if session is None:
      raise BusinessException(ErrorCode.NOT_FOUND, "会话不存在")
    await self.rag_repo.replace_session_knowledge_bases(
        session_id, knowledge_base_ids
    )

  async def toggle_pin(self, session_id: int) -> None:
    await self.rag_repo.toggle_pin(session_id)

  async def prepare_stream_message(self, session_id: int, question: str) -> int:
    session = await self.rag_repo.find_session_by_id(session_id)
    if session is None:
      raise BusinessException(ErrorCode.NOT_FOUND, "会话不存在")

    # 与 Java 版本一致：首次提问时，若标题仍为默认值，则根据知识库生成标题
    if session.message_count == 0 and session.title == "新会话":
      new_title = self._generate_title(session.knowledge_bases)
      if new_title != "新会话":
        session.title = new_title
        log.info("首次提问更新会话标题: sessionId=%d, title=%s", session_id, new_title)

    next_order = await self.rag_repo.next_message_order(session_id)
    await self.rag_repo.save_message(RagChatMessageEntity(
        session_id=session_id,
        type="USER",
        content=question,
        message_order=next_order,
        completed=True,
    ))
    assistant = await self.rag_repo.save_message(RagChatMessageEntity(
        session_id=session_id,
        type="ASSISTANT",
        content="",
        message_order=next_order + 1,
        completed=False,
    ))
    session.message_count = next_order + 2
    session.updated_at = get_beijing_now()
    await self.rag_repo.session.flush()
    return assistant.id

  async def get_stream_answer(
      self, session_id: int, question: str, history: list[dict] | None = None
  ):
    session = await self.rag_repo.find_session_by_id(session_id)
    if session is None:
      raise BusinessException(ErrorCode.NOT_FOUND, "会话不存在")
    return self.query_service.query_stream(
        query_text=question,
        knowledge_base_ids=[kb.id for kb in session.knowledge_bases],
        history=history,
    )

  async def complete_stream_message(self, message_id: int, content: str) -> None:
    await self.rag_repo.update_message_content(message_id, content)

  async def send_message(self, session_id: int, question: str) -> dict:
    assistant_id = await self.prepare_stream_message(session_id, question)
    session = await self.rag_repo.find_session_by_id(session_id)
    result = await self.query_service.query(
        query_text=question,
        knowledge_base_ids=[kb.id for kb in session.knowledge_bases] if session else [],
    )
    await self.complete_stream_message(assistant_id, result["answer"])
    return result
