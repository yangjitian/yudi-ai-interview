import os
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.models.knowledge_base import KnowledgeBaseStatus, VectorStatus


class CreateKnowledgeBaseRequest(BaseModel):
  name: str = Field(min_length=1, max_length=255)
  description: str | None = None
  skillId: str | None = Field(default=None, max_length=100)

  @field_validator("name")
  @classmethod
  def validate_name(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("知识库名称不能为空")
    return value


class UpdateKnowledgeBaseRequest(BaseModel):
  name: str = Field(min_length=1, max_length=255)
  description: str | None = None

  @field_validator("name")
  @classmethod
  def validate_name(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("知识库名称不能为空")
    return value


class KnowledgeBaseDTO(BaseModel):
  kbId: str
  name: str
  description: str | None = None
  skillId: str | None = None
  docCount: int
  status: KnowledgeBaseStatus
  createdAt: datetime
  updatedAt: datetime


class KnowledgeDocumentDTO(BaseModel):
  docId: str
  kbId: str
  filename: str
  fileSize: int
  fileType: str
  chunkCount: int
  status: VectorStatus
  errorMessage: str | None = None
  createdAt: datetime
  updatedAt: datetime


class KnowledgeSearchRequest(BaseModel):
  query: str = Field(min_length=1)
  kbId: str | None = None
  skillId: str | None = None
  topK: int = Field(
      default_factory=lambda: int(os.getenv("KNOWLEDGE_SEARCH_TOP_K", "5")),
      ge=1,
      le=50,
  )
  similarityThreshold: float = Field(
      default_factory=lambda: float(
          os.getenv("KNOWLEDGE_SIMILARITY_THRESHOLD", "0.7")
      ),
      ge=0,
      le=1,
  )

  @field_validator("query")
  @classmethod
  def validate_query(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("搜索内容不能为空")
    return value


class KnowledgeSearchResult(BaseModel):
  content: str
  similarity: float
  docId: str
  filename: str
  chunkIndex: int


class CreateSessionRequest(BaseModel):
  title: Optional[str] = None
  knowledge_base_ids: list[int] = Field(default_factory=list)


class SendMessageRequest(BaseModel):
  question: str
  include_history: bool = True


class SendMessageResponse(BaseModel):
  answer: str
  chunks: list["ChunkResult"] = Field(default_factory=list)


class KBDTO(BaseModel):
  id: int
  name: str
  category: Optional[str] = None
  vector_status: str
  chunk_count: Optional[int] = None


class SessionDTO(BaseModel):
  session_id: int
  title: str


class MessageDTO(BaseModel):
  id: int
  role: str
  content: str
  created_at: datetime


class SessionDetailDTO(BaseModel):
  id: int
  title: str
  is_pinned: bool
  knowledge_bases: list[KBDTO] = Field(default_factory=list)
  messages: list[MessageDTO] = Field(default_factory=list)
  created_at: datetime


class SessionListItemDTO(BaseModel):
  id: int
  title: str
  is_pinned: bool
  message_count: int
  knowledge_base_names: list[str] = Field(default_factory=list)
  created_at: datetime
  updated_at: Optional[datetime] = None


class UpdateTitleRequest(BaseModel):
  title: str


class UpdateKnowledgeBasesRequest(BaseModel):
  knowledge_base_ids: list[int]


class QueryRequest(BaseModel):
  query_text: str
  knowledge_base_ids: list[int] = Field(default_factory=list)
  top_k: int = 5


class ChunkResult(BaseModel):
  content: str
  score: float
  source: str


class QueryResponse(BaseModel):
  answer: str
  chunks: list[ChunkResult] = Field(default_factory=list)


class KnowledgeBaseUploadRequest(BaseModel):
  name: str
  category: Optional[str] = None


class KnowledgeBaseListItemDTO(BaseModel):
  id: int
  name: str
  category: Optional[str] = None
  original_filename: str
  file_size: Optional[int] = None
  content_type: Optional[str] = None
  uploaded_at: datetime
  last_accessed_at: Optional[datetime] = None
  access_count: int
  question_count: int
  vector_status: str
  chunk_count: Optional[int] = None
  vector_error: Optional[str] = None
