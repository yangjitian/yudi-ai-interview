import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.config.database import Base


class VoiceInterviewSessionEntity(Base):
  __tablename__ = "voice_interview_sessions"
  __table_args__ = (
    Index("idx_voice_session_id", "id", unique=True),
  )

  id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
  actual_duration: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  hr_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
  intro_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
  planned_duration: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  project_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
  tech_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
  created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  resume_id: Mapped[Optional[int]] = mapped_column(ForeignKey("resumes.id"), nullable=True)
  resumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  start_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  difficulty: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
  llm_provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
  skill_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
  evaluate_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
  current_phase: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
  custom_jd_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  evaluate_status: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
  role_type: Mapped[str] = mapped_column(String(255), nullable=False)
  status: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
  user_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class VoiceInterviewMessageEntity(Base):
  __tablename__ = "voice_interview_messages"
  __table_args__ = (
    Index("idx_voice_message_session_id", "session_id"),
  )

  id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
  sequence_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  session_id: Mapped[Optional[int]] = mapped_column(
      ForeignKey("voice_interview_sessions.id", ondelete="CASCADE"), nullable=True
  )
  timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  ai_generated_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  message_type: Mapped[str] = mapped_column(String(255), nullable=False)
  phase: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
  user_recognized_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class VoiceInterviewEvaluationEntity(Base):
  __tablename__ = "voice_interview_evaluations"

  id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
  overall_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  interview_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
  session_id: Mapped[Optional[int]] = mapped_column(
      ForeignKey("voice_interview_sessions.id", ondelete="CASCADE"), nullable=True
  )
  improvements_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  interviewer_role: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
  overall_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  question_evaluations_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  reference_answers_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  strengths_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


log = logging.getLogger(__name__)
