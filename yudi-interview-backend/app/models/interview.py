from datetime import datetime
from enum import Enum
from typing import ClassVar, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy import func as sql_func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config.database import Base
from app.models.common import AsyncTaskStatus
from app.utils.timezone_utils import get_beijing_now_naive


class SessionStatus(str, Enum):
  CREATED = "CREATED"
  IN_PROGRESS = "IN_PROGRESS"
  COMPLETED = "COMPLETED"
  EVALUATED = "EVALUATED"


class InterviewSessionEntity(Base):
  __tablename__ = "interview_sessions"
  __table_args__ = (
    Index("idx_interview_session_resume_created", "resume_id", "created_at"),
    Index("idx_interview_session_resume_status_created", "resume_id", "status", "created_at"),
    Index("idx_interview_session_skill_created", "skill_id", "created_at"),
    Index("idx_interview_session_created_at", "created_at"),
    Index("idx_session_id", "session_id", unique=True),
  )

  id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
  session_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
  skill_id: Mapped[Optional[str]] = mapped_column(
      String(64), nullable=True, default="java-backend"
  )
  difficulty: Mapped[Optional[str]] = mapped_column(
      String(16), nullable=True, default="mid"
  )
  source_type: Mapped[Optional[str]] = mapped_column(
      String(32), nullable=True, default="NORMAL"
  )
  knowledge_base_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
  interview_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
  resume_id: Mapped[Optional[int]] = mapped_column(ForeignKey("resumes.id"), nullable=True)
  total_questions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  current_question_index: Mapped[Optional[int]] = mapped_column(
      Integer, nullable=True, default=0
  )
  status: Mapped[Optional[str]] = mapped_column(
      String(20), nullable=True, default=SessionStatus.CREATED.value
  )
  questions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  # Q-02 决策前仅保留运行时语义，避免 ORM 读写 public.sql 中不存在的列。
  question_generation_fallback_reason: ClassVar[Optional[str]] = None
  overall_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  overall_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  strengths_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  improvements_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  reference_answers_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  created_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=False), nullable=False, default=get_beijing_now_naive
  )
  completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
  evaluate_status: Mapped[Optional[str]] = mapped_column(
      String(20), nullable=True
  )
  evaluate_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
  llm_provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

  answers: Mapped[list["InterviewAnswerEntity"]] = relationship(
      "InterviewAnswerEntity",
      back_populates="session",
      cascade="all, delete-orphan",
      lazy="selectin",
  )


class InterviewAnswerEntity(Base):
  __tablename__ = "interview_answers"
  __table_args__ = (
    Index("uk_answer_session_question", "session_id", "question_index", unique=True),
    Index("idx_answer_session_question", "session_id", "question_index"),
  )

  id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
  session_id: Mapped[int] = mapped_column(
      ForeignKey("interview_sessions.id"), nullable=False
  )
  question_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  question: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  category: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
  user_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  reference_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  key_points_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  # Q-02 决策前仅用于单次评估流程，持久化完成态由已有 score 字段判断。
  eval_status: ClassVar[Optional[str]] = None
  answered_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=False), nullable=False, default=get_beijing_now_naive
  )

  session: Mapped["InterviewSessionEntity"] = relationship(
      "InterviewSessionEntity", back_populates="answers"
  )
