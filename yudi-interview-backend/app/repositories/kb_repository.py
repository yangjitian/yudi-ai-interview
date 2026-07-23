from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessException, ErrorCode
from app.models.knowledge_base import KnowledgeBaseEntity, RagChatMessageEntity, RagChatSessionEntity, VectorStatus
from app.utils.timezone_utils import get_beijing_now


class KbRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def find_by_id(self, kb_id: int) -> KnowledgeBaseEntity | None:
    result = await self.session.execute(
        select(KnowledgeBaseEntity).where(KnowledgeBaseEntity.id == kb_id)
    )
    return result.scalar_one_or_none()

  async def find_by_id_with_documents(self, kb_id: int) -> KnowledgeBaseEntity | None:
    result = await self.session.execute(
        select(KnowledgeBaseEntity)
        .options(selectinload(KnowledgeBaseEntity.documents))
        .where(KnowledgeBaseEntity.id == kb_id)
    )
    return result.scalar_one_or_none()

  async def find_by_hash(self, file_hash: str) -> KnowledgeBaseEntity | None:
    result = await self.session.execute(
        select(KnowledgeBaseEntity).where(KnowledgeBaseEntity.file_hash == file_hash)
    )
    return result.scalar_one_or_none()

  async def exists_by_hash(self, file_hash: str) -> bool:
    result = await self.session.execute(
        select(KnowledgeBaseEntity.file_hash).where(
            KnowledgeBaseEntity.file_hash == file_hash
        )
    )
    return result.scalar_one_or_none() is not None

  async def save(self, entity: KnowledgeBaseEntity) -> KnowledgeBaseEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def delete(self, kb_id: int) -> bool:
    entity = await self.find_by_id(kb_id)
    if entity is None:
      return False
    await self.session.delete(entity)
    await self.session.flush()
    return True

  async def list_all(self, page: int = 1, page_size: int = 20) -> tuple[list[KnowledgeBaseEntity], int]:
    from sqlalchemy import func
    offset = (page - 1) * page_size
    count_result = await self.session.execute(
        select(func.count(KnowledgeBaseEntity.id))
    )
    total = count_result.scalar_one()
    result = await self.session.execute(
        select(KnowledgeBaseEntity)
        .where(KnowledgeBaseEntity.original_filename.is_not(None))
        .order_by(KnowledgeBaseEntity.uploaded_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    return list(result.scalars().all()), total

  async def update_vector_status(
      self, kb_id: int, status: str, error: str | None = None, chunk_count: int | None = None
  ) -> None:
    entity = await self.find_by_id(kb_id)
    if entity:
      entity.vector_status = status
      entity.vector_error = error
      if chunk_count is not None:
        entity.chunk_count = chunk_count

  async def count(self) -> int:
    """统计知识库总数"""
    result = await self.session.execute(
      select(func.count(KnowledgeBaseEntity.id))
    )
    return result.scalar_one() or 0

  async def sum_access_count(self) -> int:
    """统计所有知识库的总访问次数"""
    from sqlalchemy import func
    result = await self.session.execute(
      select(func.sum(KnowledgeBaseEntity.access_count))
    )
    value = result.scalar_one_or_none()
    return value if value is not None else 0

  async def count_by_vector_status(self, status: str) -> int:
    """按向量化状态统计数量"""
    from sqlalchemy import func
    result = await self.session.execute(
      select(func.count(KnowledgeBaseEntity.id))
      .where(KnowledgeBaseEntity.vector_status == status)
    )
    return result.scalar_one() or 0

  async def increment_question_counts(self, kb_ids: list[int]) -> None:
    """批量递增参与问答的知识库提问次数。"""
    if not kb_ids:
      return
    unique_ids = list(dict.fromkeys(kb_ids))
    result = await self.session.execute(
        select(KnowledgeBaseEntity.id).where(KnowledgeBaseEntity.id.in_(unique_ids))
    )
    existing_ids = set(result.scalars().all())
    for kb_id in unique_ids:
      if kb_id not in existing_ids:
        raise BusinessException(ErrorCode.NOT_FOUND, f"知识库不存在: {kb_id}")
    await self.session.execute(
        update(KnowledgeBaseEntity)
        .where(KnowledgeBaseEntity.id.in_(unique_ids))
        .values(question_count=KnowledgeBaseEntity.question_count + 1)
    )
    await self.session.flush()


class RagChatRepository:
  def __init__(self, session: AsyncSession):
    self.session = session

  async def find_session_by_id(self, session_id: int) -> RagChatSessionEntity | None:
    result = await self.session.execute(
        select(RagChatSessionEntity)
        .options(
            selectinload(RagChatSessionEntity.messages),
            selectinload(RagChatSessionEntity.knowledge_bases),
        )
        .where(RagChatSessionEntity.id == session_id)
    )
    return result.scalar_one_or_none()

  async def save_session(self, entity: RagChatSessionEntity) -> RagChatSessionEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def list_sessions(self) -> list[RagChatSessionEntity]:
    result = await self.session.execute(
        select(RagChatSessionEntity)
        .options(
            selectinload(RagChatSessionEntity.messages),
            selectinload(RagChatSessionEntity.knowledge_bases),
        )
        .order_by(
            RagChatSessionEntity.is_pinned.desc(),
            RagChatSessionEntity.updated_at.desc(),
        )
    )
    return list(result.scalars().all())

  async def delete_session(self, session_id: int) -> bool:
    entity = await self.find_session_by_id(session_id)
    if entity is None:
      return False
    await self.session.delete(entity)
    await self.session.flush()
    return True

  async def save_message(self, entity: RagChatMessageEntity) -> RagChatMessageEntity:
    self.session.add(entity)
    await self.session.flush()
    await self.session.refresh(entity)
    return entity

  async def next_message_order(self, session_id: int) -> int:
    result = await self.session.execute(
        select(func.max(RagChatMessageEntity.message_order)).where(
            RagChatMessageEntity.session_id == session_id
        )
    )
    return (result.scalar_one_or_none() or -1) + 1

  async def get_messages(self, session_id: int) -> list[RagChatMessageEntity]:
    result = await self.session.execute(
        select(RagChatMessageEntity)
        .where(RagChatMessageEntity.session_id == session_id)
        .order_by(RagChatMessageEntity.message_order)
    )
    return list(result.scalars().all())

  async def update_session_title(self, session_id: int, title: str) -> None:
    entity = await self.find_session_by_id(session_id)
    if entity:
      entity.title = title
      entity.updated_at = get_beijing_now()

  async def toggle_pin(self, session_id: int) -> bool:
    entity = await self.find_session_by_id(session_id)
    if entity:
      entity.is_pinned = not entity.is_pinned
      return entity.is_pinned
    return False

  async def update_message_content(self, message_id: int, content: str) -> None:
    from sqlalchemy import update
    await self.session.execute(
        update(RagChatMessageEntity)
        .where(RagChatMessageEntity.id == message_id)
        .values(content=content, completed=True, updated_at=get_beijing_now())
    )

  async def replace_session_knowledge_bases(
      self, session_id: int, knowledge_base_ids: list[int]
  ) -> None:
    session = await self.find_session_by_id(session_id)
    if session is None:
      return
    if not knowledge_base_ids:
      session.knowledge_bases = []
      return
    result = await self.session.execute(
      select(KnowledgeBaseEntity).where(
          KnowledgeBaseEntity.id.in_(knowledge_base_ids)
      )
    )
    session.knowledge_bases = list(result.scalars().all())

  async def count_by_type(self, msg_type: str) -> int:
    """按消息类型统计数量（如 USER、ASSISTANT）"""
    result = await self.session.execute(
      select(func.count(RagChatMessageEntity.id))
      .where(RagChatMessageEntity.type == msg_type)
    )
    return result.scalar_one() or 0
