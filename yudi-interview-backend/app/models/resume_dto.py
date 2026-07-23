from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.common import AsyncTaskStatus

class StrengthItemDTO(BaseModel):
  category: str
  description: str


class SuggestionItemDTO(BaseModel):
  category: str
  priority: str
  issue: str
  recommendation: str


class ResumeListItemDTO(BaseModel):
  id: int
  filename: str
  file_size: Optional[int] = None
  uploaded_at: datetime
  access_count: int
  latest_score: Optional[int] = None
  last_analyzed_at: Optional[datetime] = None
  interview_count: int = 0
  analyze_status: AsyncTaskStatus = AsyncTaskStatus.PENDING
  analyze_error: Optional[str] = None


class AnalysisHistoryDTO(BaseModel):
  id: int
  overall_score: Optional[int] = None
  content_score: Optional[int] = None
  structure_score: Optional[int] = None
  skill_match_score: Optional[int] = None
  expression_score: Optional[int] = None
  project_score: Optional[int] = None
  summary: Optional[str] = None
  analyzed_at: datetime
  strengths: list[StrengthItemDTO] = Field(default_factory=list)
  suggestions: list[SuggestionItemDTO] = Field(default_factory=list)


class InterviewHistoryItemDTO(BaseModel):
  session_id: int
  skill_id: str
  overall_score: Optional[int] = None
  completed_at: Optional[datetime] = None


class ResumeDetailDTO(BaseModel):
  id: int
  filename: str
  file_size: Optional[int] = None
  content_type: Optional[str] = None
  storage_url: Optional[str] = None
  uploaded_at: datetime
  access_count: int
  resume_text: Optional[str] = None
  analyze_status: AsyncTaskStatus
  analyze_error: Optional[str] = None
  analyses: list[AnalysisHistoryDTO] = Field(default_factory=list)
  interviews: list[InterviewHistoryItemDTO] = Field(default_factory=list)


class ResumeUploadResponseDTO(BaseModel):
  id: int
  analyze_status: AsyncTaskStatus
  message: str = "简历上传成功，分析中"


class ResumeAnalysisResultDTO(BaseModel):
  id: int
  overall_score: int
  content_score: int
  structure_score: int
  skill_match_score: int
  expression_score: int
  project_score: int
  summary: str
  strengths: list[StrengthItemDTO] = Field(default_factory=list)
  suggestions: list[SuggestionItemDTO] = Field(default_factory=list)
  analyzed_at: datetime


class ReanalyzeRequest(BaseModel):
  force: bool = False
