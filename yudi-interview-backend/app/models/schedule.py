from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.config.database import Base
from app.utils.timezone_utils import get_beijing_now


class InterviewStatus(str, Enum):
  PENDING = "PENDING"
  COMPLETED = "COMPLETED"
  CANCELLED = "CANCELLED"
  RESCHEDULED = "RESCHEDULED"


class InterviewScheduleEntity(Base):
  __tablename__ = "interview_schedule"
  __table_args__ = (
      CheckConstraint(
          "status IN ('PENDING', 'COMPLETED', 'CANCELLED', 'RESCHEDULED')",
          name="interview_schedule_status_check",
      ),
  )

  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
  company_name: Mapped[str] = mapped_column(String(255), nullable=False)
  position: Mapped[str] = mapped_column(String(255), nullable=False)
  interview_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
  interview_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
  meeting_link: Mapped[str | None] = mapped_column(Text, nullable=True)
  round_number: Mapped[int] = mapped_column(default=1)
  interviewer: Mapped[str | None] = mapped_column(String(255), nullable=True)
  notes: Mapped[str | None] = mapped_column(Text, nullable=True)
  status: Mapped[str] = mapped_column(
      String(255), nullable=False, default=InterviewStatus.PENDING.value
  )
  created_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True), nullable=False, default=get_beijing_now
  )
  updated_at: Mapped[datetime] = mapped_column(
      DateTime(timezone=True), nullable=False, default=get_beijing_now, onupdate=get_beijing_now
  )
