from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy import func as sql_func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config.database import Base
from app.models.common import AsyncTaskStatus
from app.utils.timezone_utils import get_beijing_now


class ResumeEntity(Base):
  __tablename__ = "resumes"
  __table_args__ = (
    Index("idx_resume_hash", "file_hash", unique=True),
  )

  id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
  file_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
  original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
  file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  content_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
  storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
  storage_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
  resume_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  uploaded_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True),
      nullable=False,
      default=get_beijing_now,
  )
  last_accessed_at: Mapped[Optional[datetime]] = mapped_column(
      DateTime(timezone=True), nullable=True
  )
  access_count: Mapped[int] = mapped_column(Integer, default=1)
  analyze_status: Mapped[str] = mapped_column(
      String(20), default=AsyncTaskStatus.PENDING.value
  )
  analyze_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

  analyses: Mapped[list["ResumeAnalysisEntity"]] = relationship(
      "ResumeAnalysisEntity",
      back_populates="resume",
      cascade="all, delete-orphan",
      lazy="selectin",
  )


class ResumeAnalysisEntity(Base):
  __tablename__ = "resume_analyses"

  id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
  resume_id: Mapped[int] = mapped_column(
      ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False
  )
  overall_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  content_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  structure_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  skill_match_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  expression_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  project_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  strengths_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  suggestions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
  analyzed_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True),
      nullable=False,
      default=get_beijing_now,
  )

  resume: Mapped["ResumeEntity"] = relationship(
      "ResumeEntity", back_populates="analyses"
  )
