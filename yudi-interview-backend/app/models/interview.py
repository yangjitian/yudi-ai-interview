from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy import func as sql_func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config.database import Base
from app.models.common import AsyncTaskStatus
from app.utils.timezone_utils import get_beijing_now


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
  skill_id: Mapped[str] = mapped_column(String(64), default="java-backend")
  difficulty: Mapped[str] = mapped_column(String(16), default="mid")
  resume_id: Mapped[Optional[int]] = mapped_column(ForeignKey("resumes.id"), nullable=True)
  total_questions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  current_question_index: Mapped[int] = mapped_column(Integer, default=0)
  status: Mapped[str] = mapped_column(String(20), default=SessionStatus.CREATED.value)
  questions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  question_generation_fallback_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
  overall_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  overall_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  strengths_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  improvements_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  reference_answers_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  created_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True), nullable=False, default=get_beijing_now
  )
  completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  evaluate_status: Mapped[Optional[str]] = mapped_column(
      String(50), nullable=True
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
      ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False
  )
  question_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  question: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
  user_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  reference_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  key_points_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  eval_status: Mapped[Optional[str]] = mapped_column(
      String(20),
      nullable=True,
      default=None,
      comment="??????: None=???, EVALUATING=???, COMPLETED=??, FAILED=??",
  )
  answered_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True), nullable=False, default=get_beijing_now
  )

  session: Mapped["InterviewSessionEntity"] = relationship(
      "InterviewSessionEntity", back_populates="answers"
  )
