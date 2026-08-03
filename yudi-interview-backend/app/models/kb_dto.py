import os
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.knowledge_base import (
    KnowledgeBaseQuestionEntity,
    KnowledgeBaseStatus,
    VectorStatus,
)


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


class KnowledgeBaseQuestionStatus(str, Enum):
  DRAFT = "DRAFT"
  ACTIVE = "ACTIVE"
  ARCHIVED = "ARCHIVED"
  STALE = "STALE"


class KnowledgeBaseQuestionFollowUpDTO(BaseModel):
  question: str | None = None
  referenceAnswer: str | None = None
  keyPoints: list[str | None] | None = None
  scoringRubric: str | None = None


class CreateKnowledgeBaseQuestionRequest(BaseModel):
  difficulty: str | None = None
  type: str | None = None
  category: str = Field(min_length=1)
  question: str = Field(min_length=1)
  topicSummary: str | None = None
  referenceAnswer: str | None = None
  keyPoints: list[str | None] | None = None
  scoringRubric: str | None = None
  followUps: list[KnowledgeBaseQuestionFollowUpDTO | None] | None = None
  sourceContext: str | None = None
  status: KnowledgeBaseQuestionStatus | None = None

  @field_validator("category")
  @classmethod
  def validate_category(cls, value: str) -> str:
    if not value.strip():
      raise ValueError("面试方向不能为空")
    return value

  @field_validator("question")
  @classmethod
  def validate_question(cls, value: str) -> str:
    if not value.strip():
      raise ValueError("题干不能为空")
    return value


class UpdateKnowledgeBaseQuestionRequest(BaseModel):
  difficulty: str | None = None
  type: str | None = None
  category: str | None = None
  question: str | None = None
  topicSummary: str | None = None
  referenceAnswer: str | None = None
  keyPoints: list[str | None] | None = None
  scoringRubric: str | None = None
  followUps: list[KnowledgeBaseQuestionFollowUpDTO | None] | None = None
  sourceContext: str | None = None
  status: KnowledgeBaseQuestionStatus | None = None


class UpdateKnowledgeBaseQuestionStatusRequest(BaseModel):
  status: KnowledgeBaseQuestionStatus


class KnowledgeBaseQuestionDTO(BaseModel):
  id: int
  knowledgeBaseId: int | None = None
  knowledgeBaseName: str | None = None
  skillId: str
  difficulty: str | None = None
  type: str | None = None
  category: str | None = None
  question: str
  topicSummary: str | None = None
  referenceAnswer: str | None = None
  keyPoints: list[str] = Field(default_factory=list)
  scoringRubric: str | None = None
  followUps: list[KnowledgeBaseQuestionFollowUpDTO] = Field(default_factory=list)
  sourceContext: str | None = None
  status: KnowledgeBaseQuestionStatus
  createdAt: datetime
  updatedAt: datetime


class KnowledgeBaseQuestionCategoryCount(BaseModel):
  category: str
  count: int


class QuestionGenerationStatus(str, Enum):
  NONE = "NONE"
  QUEUED = "QUEUED"
  PROCESSING = "PROCESSING"
  COMPLETED = "COMPLETED"
  FAILED = "FAILED"
  CANCELLED = "CANCELLED"


class QuestionGenerationConfig(BaseModel):
  difficulty: str
  questionCount: int
  followUpCount: int
  categoryLimit: int
  llmProvider: str | None = None


class SubmitQuestionGenerationCommand(BaseModel):
  kb_id: int
  config: QuestionGenerationConfig


class CompleteQuestionGenerationCommand(BaseModel):
  model_config = ConfigDict(arbitrary_types_allowed=True)

  kb_id: int
  task_id: str
  questions: list[KnowledgeBaseQuestionEntity]
  saved_count: int = Field(ge=0)
  skipped_count: int = Field(ge=0)
  message: str


class QuestionGenerationStatusResponse(BaseModel):
  knowledgeBaseId: int
  questionGenStatus: QuestionGenerationStatus
  questionGenTaskId: str | None = None
  questionGenConfig: QuestionGenerationConfig | None = None
  savedCount: int
  skippedCount: int
  message: str | None = None
  error: str | None = None
  updatedAt: datetime | None = None


class GeneratedKnowledgeBaseQuestion(BaseModel):
  category: str | None = None
  type: str | None = None
  question: str | None = None
  topicSummary: str | None = None
  referenceAnswer: str | None = None
  keyPoints: list[str | None] | None = None
  scoringRubric: str | None = None
  followUps: list[KnowledgeBaseQuestionFollowUpDTO | None] | None = None


class GeneratedKnowledgeBaseQuestionList(BaseModel):
  questions: list[GeneratedKnowledgeBaseQuestion | None] | None = None


class CreateKnowledgeBaseInterviewRequest(BaseModel):
  knowledgeBaseId: int
  category: str | None = None
  difficulty: str | None = None
  mainQuestionCount: int = Field(ge=1, le=20)
  followUpCount: int = Field(default=0, ge=0, le=5)
  llmProvider: str | None = None


class KnowledgeBaseInterviewCategoryOption(BaseModel):
  category: str
  availableQuestionCount: int


class KnowledgeBaseInterviewFollowUpOption(BaseModel):
  followUpCount: int
  availableQuestionCount: int
  selectable: bool


class KnowledgeBaseInterviewCapacityResponse(BaseModel):
  knowledgeBaseId: int
  category: str | None = None
  difficulty: str
  mainQuestionCount: int
  categories: list[KnowledgeBaseInterviewCategoryOption] = Field(default_factory=list)
  followUpOptions: list[KnowledgeBaseInterviewFollowUpOption] = Field(default_factory=list)


class KnowledgeBaseInterviewQuestionResponse(BaseModel):
  questionIndex: int
  question: str
  type: str | None = None
  category: str
  topicSummary: str | None = None
  userAnswer: str | None = None
  score: int | None = None
  feedback: str | None = None
  isFollowUp: bool = False
  parentQuestionIndex: int | None = None
  referenceAnswer: str | None = None
  keyPoints: list[str] | None = None
  scoringRubric: str | None = None
  sourceContext: str | None = None


class KnowledgeBaseInterviewSessionResponse(BaseModel):
  sessionId: str
  resumeText: str
  totalQuestions: int
  currentQuestionIndex: int
  questions: list[KnowledgeBaseInterviewQuestionResponse] = Field(default_factory=list)
  status: str
  knowledgeBaseId: int | None = None
  interviewCategory: str | None = None
