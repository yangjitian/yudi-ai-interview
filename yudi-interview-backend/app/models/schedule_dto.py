from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models.schedule import InterviewStatus


class CreateScheduleRequest(BaseModel):
  companyName: str = Field(min_length=1)
  position: str = Field(min_length=1)
  interviewTime: datetime
  interviewType: Literal["ONSITE", "VIDEO", "PHONE"] | None = None
  meetingLink: str | None = None
  roundNumber: int = Field(default=1, ge=1)
  interviewer: str | None = None
  notes: str | None = None

  @field_validator("companyName", "position")
  @classmethod
  def validate_required_text(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("字段不能为空")
    return value


class UpdateScheduleRequest(BaseModel):
  companyName: str | None = None
  position: str | None = None
  interviewTime: datetime | None = None
  interviewType: Literal["ONSITE", "VIDEO", "PHONE"] | None = None
  meetingLink: str | None = None
  roundNumber: int | None = Field(default=None, ge=1)
  interviewer: str | None = None
  notes: str | None = None


class ScheduleDTO(BaseModel):
  id: int
  companyName: str
  position: str
  interviewTime: datetime
  interviewType: str | None = None
  meetingLink: str | None = None
  roundNumber: int
  interviewer: str | None = None
  notes: str | None = None
  status: InterviewStatus
  createdAt: datetime
  updatedAt: datetime


class ScheduleListItemDTO(BaseModel):
  id: int
  companyName: str
  position: str
  interviewTime: datetime
  interviewType: str | None = None
  roundNumber: int
  status: InterviewStatus


class ParseRequest(BaseModel):
  rawText: str = Field(min_length=1)
  source: Literal["feishu", "tencent", "zoom", "other"] | None = None


class ParseResponse(BaseModel):
  success: bool
  data: CreateScheduleRequest | None = None
  confidence: float
  parseMethod: Literal["rule", "ai", "none"]
  log: str
