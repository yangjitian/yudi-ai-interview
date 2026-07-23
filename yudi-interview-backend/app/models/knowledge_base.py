from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, BigInteger, Column, DateTime, ForeignKey, Index, Integer, String, Table, Text
from sqlalchemy import func as sql_func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config.database import Base
from app.utils.timezone_utils import get_beijing_now


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
  __tablename__ = "knowledge_bases"
  __table_args__ = (
      Index("idx_kb_hash", "file_hash", unique=True),
      Index("idx_kb_category", "category"),
      Index("idx_kb_business_id", "kb_id", unique=True),
      Index("idx_kb_skill_id", "skill_id"),
  )

  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
  kb_id: Mapped[str] = mapped_column(
      String(36), nullable=False, unique=True, default=lambda: str(uuid4())
  )
  name: Mapped[str] = mapped_column(String(255), nullable=False)
  description: Mapped[str | None] = mapped_column(Text, nullable=True)
  skill_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
  doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
  status: Mapped[str] = mapped_column(
      String(20), nullable=False, default=KnowledgeBaseStatus.ACTIVE.value
  )
  created_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True), nullable=False, default=get_beijing_now
  )
  updated_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True), nullable=False, default=get_beijing_now,
      onupdate=get_beijing_now
  )

  # 兼容原有“单文件即知识库”数据与接口。
  file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
  category: Mapped[str | None] = mapped_column(String(100), nullable=True)
  original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
  file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
  content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
  storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
  storage_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
  uploaded_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True), nullable=False, default=get_beijing_now
  )
  last_accessed_at: Mapped[datetime | None] = mapped_column(
      DateTime(timezone=True), nullable=True
  )
  access_count: Mapped[int] = mapped_column(Integer, default=1)
  question_count: Mapped[int] = mapped_column(Integer, default=0)
  vector_status: Mapped[str] = mapped_column(
      String(20), default=VectorStatus.PENDING.value
  )
  vector_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
  chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
  content_text: Mapped[str | None] = mapped_column(Text, nullable=True)

  documents: Mapped[list["KnowledgeDocumentEntity"]] = relationship(
      "KnowledgeDocumentEntity",
      back_populates="knowledge_base",
      cascade="all, delete-orphan",
      passive_deletes=True,
  )


class KnowledgeDocumentEntity(Base):
  __tablename__ = "knowledge_documents"
  __table_args__ = (
      Index("idx_knowledge_document_doc_id", "doc_id", unique=True),
      Index("idx_knowledge_document_kb_id", "kb_id"),
      Index("idx_knowledge_document_status", "status"),
  )

  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
  doc_id: Mapped[str] = mapped_column(
      String(36), nullable=False, unique=True, default=lambda: str(uuid4())
  )
  kb_id: Mapped[int] = mapped_column(
      ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
  )
  filename: Mapped[str] = mapped_column(String(255), nullable=False)
  file_key: Mapped[str] = mapped_column(String(500), nullable=False)
  file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
  file_type: Mapped[str] = mapped_column(String(20), nullable=False)
  chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
  status: Mapped[str] = mapped_column(
      String(20), nullable=False, default=VectorStatus.PENDING.value
  )
  error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
  created_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True), nullable=False, default=get_beijing_now
  )
  updated_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True), nullable=False, default=get_beijing_now,
      onupdate=get_beijing_now
  )

  knowledge_base: Mapped[KnowledgeBaseEntity] = relationship(
      "KnowledgeBaseEntity", back_populates="documents"
  )
  chunks: Mapped[list["KnowledgeChunkEntity"]] = relationship(
      "KnowledgeChunkEntity",
      back_populates="document",
      cascade="all, delete-orphan",
      passive_deletes=True,
  )


class KnowledgeChunkEntity(Base):
  __tablename__ = "knowledge_chunks"
  __table_args__ = (
      Index("idx_knowledge_chunk_doc_id", "doc_id"),
      Index("idx_knowledge_chunk_kb_id", "kb_id"),
      Index("idx_knowledge_chunk_order", "doc_id", "chunk_index", unique=True),
  )

  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
  chunk_id: Mapped[str] = mapped_column(
      String(36), nullable=False, unique=True, default=lambda: str(uuid4())
  )
  doc_id: Mapped[int] = mapped_column(
      ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False
  )
  kb_id: Mapped[int] = mapped_column(
      ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
  )
  content: Mapped[str] = mapped_column(Text, nullable=False)
  chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
  embedding: Mapped[list[float] | None] = mapped_column(
      Vector(EMBEDDING_DIMENSION), nullable=True
  )
  created_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True), nullable=False, default=get_beijing_now
  )

  document: Mapped[KnowledgeDocumentEntity] = relationship(
      "KnowledgeDocumentEntity", back_populates="chunks"
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
      DateTime(timezone=True), nullable=False, default=get_beijing_now
  )
  updated_at: Mapped[Optional[datetime]] = mapped_column(
      DateTime(timezone=True), nullable=True, default=get_beijing_now
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
      DateTime(timezone=True), nullable=False, default=get_beijing_now
  )
  updated_at: Mapped[datetime | None] = mapped_column(
      DateTime(timezone=True), nullable=True, default=get_beijing_now
  )

  session: Mapped[RagChatSessionEntity] = relationship(
      "RagChatSessionEntity", back_populates="messages"
  )
