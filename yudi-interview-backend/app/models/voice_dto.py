from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from typing import Optional


class CreateVoiceSessionRequest(BaseModel):
  model_config = ConfigDict(populate_by_name=True)

  skill_id: str = Field(default="java-backend", validation_alias="skillId")
  difficulty: str = "mid"
  question_count: int = Field(
      default=5, ge=3, le=20, validation_alias="questionCount"
  )
  resume_id: Optional[int] = Field(default=None, validation_alias="resumeId")
  role_type: Optional[str] = Field(default=None, validation_alias="roleType")
  intro_enabled: bool = Field(default=False, validation_alias="introEnabled")
  tech_enabled: bool = Field(default=True, validation_alias="techEnabled")
  project_enabled: bool = Field(default=True, validation_alias="projectEnabled")
  hr_enabled: bool = Field(default=True, validation_alias="hrEnabled")
  planned_duration: Optional[int] = Field(
      default=None, validation_alias="plannedDuration"
  )
  llm_provider: Optional[str] = Field(default=None, validation_alias="llmProvider")
  custom_jd_text: Optional[str] = Field(
      default=None, validation_alias="customJdText"
  )


class VoiceSessionResponseDTO(BaseModel):
  session_id: int
  status: str
  current_phase: Optional[str] = None
  planned_duration: Optional[int] = None
  web_socket_url: Optional[str] = None


class VoiceSessionMetaDTO(BaseModel):
  model_config = ConfigDict(populate_by_name=True)

  session_id: int = Field(serialization_alias="sessionId")
  role_type: str = Field(serialization_alias="roleType")
  skill_id: Optional[str] = None
  difficulty: Optional[str] = None
  status: Optional[str] = None
  current_phase: Optional[str] = Field(default=None, serialization_alias="currentPhase")
  planned_duration: Optional[int] = Field(default=None, serialization_alias="plannedDuration")
  actual_duration: Optional[int] = Field(default=None, serialization_alias="actualDuration")
  created_at: Optional[str] = Field(default=None, serialization_alias="createdAt")
  updated_at: Optional[str] = Field(default=None, serialization_alias="updatedAt")
  start_time: Optional[str] = None
  end_time: Optional[str] = None
  evaluate_status: Optional[str] = Field(default=None, serialization_alias="evaluateStatus")
  evaluate_error: Optional[str] = Field(default=None, serialization_alias="evaluateError")
  # 评估总分与详情页同源（voice_interview_evaluations.overall_score），避免列表/详情数据不一致
  overall_score: Optional[int] = Field(default=None, serialization_alias="overallScore")
  message_count: int = Field(default=0, serialization_alias="messageCount")


class VoiceInterviewMessageDTO(BaseModel):
  id: int
  session_id: int
  message_type: str
  phase: Optional[str] = None
  user_recognized_text: str = ""
  ai_generated_text: str = ""
  timestamp: Optional[str] = None
  sequence_num: Optional[int] = None


class VoiceEvaluationAnswerDetail(BaseModel):
  model_config = ConfigDict(populate_by_name=True)

  question_index: int = Field(serialization_alias="questionIndex")
  question: str
  category: str
  user_answer: Optional[str] = Field(default=None, serialization_alias="userAnswer")
  score: int
  feedback: str
  reference_answer: Optional[str] = Field(
      default=None, serialization_alias="referenceAnswer"
  )
  key_points: list[str] = Field(
      default_factory=list, serialization_alias="keyPoints"
  )


class VoiceEvaluationDetailDTO(BaseModel):
  model_config = ConfigDict(populate_by_name=True)

  session_id: int = Field(serialization_alias="sessionId")
  total_questions: int = Field(serialization_alias="totalQuestions")
  overall_score: Optional[int] = Field(default=None, serialization_alias="overallScore")
  overall_feedback: Optional[str] = Field(
      default=None, serialization_alias="overallFeedback"
  )
  strengths: list[str] = Field(default_factory=list)
  improvements: list[str] = Field(default_factory=list)
  answers: list[VoiceEvaluationAnswerDetail] = Field(default_factory=list)


class VoiceEvaluationStatusDTO(BaseModel):
  model_config = ConfigDict(populate_by_name=True)

  evaluate_status: Optional[str] = Field(
      default=None, serialization_alias="evaluateStatus"
  )
  evaluate_error: Optional[str] = Field(
      default=None, serialization_alias="evaluateError"
  )
  evaluate_status_updated_at: Optional[datetime] = Field(
      default=None, serialization_alias="evaluateStatusUpdatedAt"
  )
  evaluation: Optional[VoiceEvaluationDetailDTO] = None


class WebSocketControlMessage(BaseModel):
  type: str
  session_id: Optional[int] = None
  action: Optional[str] = None
  phase: Optional[str] = None
  data: Optional[dict[str, object]] = None


class WebSocketSubtitleMessage(BaseModel):
  session_id: int
  text: str
  is_final: bool = False
