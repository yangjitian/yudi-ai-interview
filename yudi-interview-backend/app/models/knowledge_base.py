from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Table, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID as PG_UUID, JSON
from sqlalchemy import func as sql_func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config.database import Base
from app.utils.timezone_utils import get_beijing_now_naive


EMBEDDING_DIMENSION = 1024


class VectorStatus(str, Enum):
  PENDING = "PENDING"
  PROCESSING = "PROCESSING"
  COMPLETED = "COMPLETED"
  FAILED = "FAILED"


class KnowledgeBaseStatus(str, Enum):
  ACTIVE = "ACTIVE"
  DISABLED = "DISABLED"


class KnowledgeBaseEntity(Base):
  """知识库实体，与 Java 版 KnowledgeBaseEntity 对齐。

  注意：数据库中不存在 kb_id / description / skill_id / doc_count / status /
  created_at / updated_at / content_text 列，模型中不得定义这些字段。
  """
  __tablename__ = "knowledge_bases"
  __table_args__ = (
      CheckConstraint(
          "question_gen_status IN ('NONE', 'QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED')",
          name="knowledge_bases_question_gen_status_check",
      ),
      Index("idx_kb_hash", "file_hash", unique=True),
      Index("idx_kb_category", "category"),
      Index(
          "idx_kb_question_gen_status_updated",
          "question_gen_status",
          "question_gen_updated_at",
      ),
  )

  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
  name: Mapped[str] = mapped_column(String(255), nullable=False)
  file_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
  category: Mapped[str | None] = mapped_column(String(100), nullable=True)
  original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
  file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
  content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
  storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
  storage_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
  uploaded_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=False), nullable=False, default=get_beijing_now_naive
  )
  last_accessed_at: Mapped[datetime | None] = mapped_column(
      DateTime(timezone=False), nullable=True
  )
  access_count: Mapped[int] = mapped_column(Integer, default=0)
  question_count: Mapped[int] = mapped_column(Integer, default=0)
  vector_status: Mapped[str] = mapped_column(
      String(20), default=VectorStatus.PENDING.value
  )
  vector_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
  chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
  question_gen_status: Mapped[str] = mapped_column(
      String(20), nullable=False, default="NONE", server_default="NONE"
  )
  question_gen_error: Mapped[str | None] = mapped_column(
      String(500), nullable=True
  )
  question_gen_task_id: Mapped[str | None] = mapped_column(
      String(36), nullable=True
  )
  question_gen_config: Mapped[str | None] = mapped_column(Text, nullable=True)
  question_gen_message: Mapped[str | None] = mapped_column(
      String(500), nullable=True
  )
  question_gen_saved_count: Mapped[int] = mapped_column(
      Integer, nullable=False, default=0, server_default="0"
  )
  question_gen_skipped_count: Mapped[int] = mapped_column(
      Integer, nullable=False, default=0, server_default="0"
  )
  question_gen_updated_at: Mapped[datetime | None] = mapped_column(
      TIMESTAMP(precision=6, timezone=False), nullable=True
  )


class KnowledgeBaseQuestionEntity(Base):
  __tablename__ = "knowledge_base_questions"
  __table_args__ = (
      CheckConstraint(
          "status::text = ANY (ARRAY['DRAFT', 'ACTIVE', 'ARCHIVED', 'STALE']::text[])",
          name="knowledge_base_questions_status_check",
      ),
      Index("idx_kb_question_kb_status", "knowledge_base_id", "status"),
      Index("idx_kb_question_skill_difficulty", "skill_id", "difficulty"),
  )

  created_at: Mapped[datetime] = mapped_column(
      TIMESTAMP(precision=6, timezone=False),
      nullable=False,
      default=get_beijing_now_naive,
  )
  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
  # 数据库层允许为空，业务层由 Service 保证创建题目时非空。
  knowledge_base_id: Mapped[int | None] = mapped_column(
      BigInteger,
      ForeignKey(
          "knowledge_bases.id",
          name="fkosobqu06r3tbr13ca043slftw",
      ),
      nullable=True,
  )
  updated_at: Mapped[datetime] = mapped_column(
      TIMESTAMP(precision=6, timezone=False),
      nullable=False,
      default=get_beijing_now_naive,
      onupdate=get_beijing_now_naive,
  )
  difficulty: Mapped[str | None] = mapped_column(String(16), nullable=True)
  status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
  category: Mapped[str | None] = mapped_column(String(64), nullable=True)
  kb_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
  skill_id: Mapped[str] = mapped_column(String(64), nullable=False, default="knowledge-base")
  type: Mapped[str | None] = mapped_column(String(64), nullable=True)
  topic_summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
  follow_ups_json: Mapped[str | None] = mapped_column(Text, nullable=True)
  key_points_json: Mapped[str | None] = mapped_column(Text, nullable=True)
  question: Mapped[str] = mapped_column(Text, nullable=False)
  reference_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
  scoring_rubric: Mapped[str | None] = mapped_column(Text, nullable=True)
  source_context: Mapped[str | None] = mapped_column(Text, nullable=True)


class VectorStoreEntity(Base):
  """映射到已有的 vector_store 表（与 Java Spring AI PgVectorStore 一致）。

  数据库中实际存在此表，metadata JSON 中通过 kb_id 关联知识库。
  """
  __tablename__ = "vector_store"

  id: Mapped[str] = mapped_column(
      PG_UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
  )
  content: Mapped[str | None] = mapped_column(Text, nullable=True)
  metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
  embedding: Mapped[list[float] | None] = mapped_column(
      Vector(EMBEDDING_DIMENSION), nullable=True
  )


rag_session_knowledge_bases = Table(
    "rag_session_knowledge_bases",
    Base.metadata,
    Column(
        "knowledge_base_id",
        BigInteger,
        ForeignKey("knowledge_bases.id", ondelete="NO ACTION"),
        primary_key=True,
    ),
    Column(
        "session_id",
        BigInteger,
        ForeignKey("rag_chat_sessions.id", ondelete="NO ACTION"),
        primary_key=True,
    ),
)


class RagChatSessionEntity(Base):
  __tablename__ = "rag_chat_sessions"
  __table_args__ = (Index("idx_rag_session_created", "created_at"),)

  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
  title: Mapped[str] = mapped_column(String(255), nullable=False, default="新会话")
  status: Mapped[str | None] = mapped_column(String(20), nullable=True, default="ACTIVE")
  message_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
  is_pinned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
  created_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=False), nullable=False, default=get_beijing_now_naive
  )
  updated_at: Mapped[Optional[datetime]] = mapped_column(
      DateTime(timezone=False), nullable=True, default=get_beijing_now_naive
  )

  knowledge_bases: Mapped[list[KnowledgeBaseEntity]] = relationship(
      "KnowledgeBaseEntity",
      secondary=rag_session_knowledge_bases,
      lazy="selectin",
  )

  messages: Mapped[list["RagChatMessageEntity"]] = relationship(
      "RagChatMessageEntity",
      back_populates="session",
      cascade="all, delete-orphan",
      lazy="selectin",
      order_by="RagChatMessageEntity.message_order",
  )


class RagChatMessageEntity(Base):
  __tablename__ = "rag_chat_messages"
  __table_args__ = (
      Index("idx_rag_message_session", "session_id"),
      Index("idx_rag_message_order", "session_id", "message_order"),
  )

  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
  session_id: Mapped[int] = mapped_column(
      BigInteger,
      ForeignKey("rag_chat_sessions.id", ondelete="NO ACTION"),
      nullable=False,
  )
  type: Mapped[str] = mapped_column(String(20), nullable=False)
  content: Mapped[str] = mapped_column(Text, nullable=False)
  message_order: Mapped[int] = mapped_column(Integer, nullable=False)
  completed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
  created_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=False), nullable=False, default=get_beijing_now_naive
  )
  updated_at: Mapped[datetime | None] = mapped_column(
      DateTime(timezone=False), nullable=True, default=get_beijing_now_naive
  )

  session: Mapped[RagChatSessionEntity] = relationship(
      "RagChatSessionEntity", back_populates="messages"
  )
