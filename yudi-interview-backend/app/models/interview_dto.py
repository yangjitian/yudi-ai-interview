from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CreateInterviewRequest(BaseModel):
  resume_text: Optional[str] = None
  question_count: int = Field(default=5, ge=3, le=20)
  resume_id: Optional[int] = None
  force_create: bool = False
  llm_provider: Optional[str] = None
  skill_id: str = "java-backend"
  difficulty: str = "mid"
  custom_categories: list[dict] = Field(default_factory=list)
  jd_text: Optional[str] = None


class InterviewQuestionDTO(BaseModel):
  question: str
  category: str
  answer: Optional[str] = None


class InterviewSessionDTO(BaseModel):
  session_id: str
  resume_text: str = ""
  total_questions: int
  current_index: int
  questions: list[InterviewQuestionDTO] = Field(default_factory=list)
  status: str = "CREATED"
  is_fallback: bool = False
  fallback_reason: Optional[str] = None
  generation_mode: str = "llm"


class SubmitAnswerRequest(BaseModel):
  question_index: int
  answer: str


class SubmitAnswerResponse(BaseModel):
  has_next_question: bool
  next_question: Optional[InterviewQuestionDTO] = None
  new_index: int
  total_questions: int


class CategoryScore(BaseModel):
  category: str
  score: int
  question_count: int


class QuestionEvaluation(BaseModel):
  question_index: int
  question: str
  category: str
  score: int
  feedback: str
  reference_answer: Optional[str] = None
  key_points: list[str] = Field(default_factory=list)
  eval_status: Optional[str] = None


class InterviewReportDTO(BaseModel):
  session_id: str
  overall_score: int
  category_scores: list[CategoryScore] = Field(default_factory=list)
  question_evaluations: list[QuestionEvaluation] = Field(default_factory=list)
  overall_feedback: str = ""
  strengths: list[str] = Field(default_factory=list)
  improvements: list[str] = Field(default_factory=list)
  reference_answers: list[dict] = Field(default_factory=list)


class SessionListItemDTO(BaseModel):
  id: int
  session_id: str
  skill_id: str
  difficulty: str
  resume_id: Optional[int] = None
  total_questions: int
  overall_score: Optional[int] = None
  status: str
  evaluate_status: Optional[str] = None
  evaluate_error: Optional[str] = None
  created_at: datetime
  completed_at: Optional[datetime] = None


class InterviewDetailDTO(BaseModel):
  session_id: str
  skill_id: str
  difficulty: str
  total_questions: int
  current_index: int
  overall_score: Optional[int] = None
  overall_feedback: Optional[str] = None
  status: str
  evaluate_status: Optional[str] = None
  evaluate_error: Optional[str] = None
  questions: list[InterviewQuestionDTO] = Field(default_factory=list)
  answers: list[dict] = Field(default_factory=list)
  report: Optional[InterviewReportDTO] = None
  created_at: datetime
  completed_at: Optional[datetime] = None
