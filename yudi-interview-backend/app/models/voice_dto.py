from pydantic import BaseModel, Field
from typing import Optional


class CreateVoiceSessionRequest(BaseModel):
  skill_id: str = "java-backend"
  difficulty: str = "mid"
  question_count: int = Field(default=5, ge=3, le=20)
  resume_id: Optional[int] = None
  role_type: Optional[str] = None
  intro_enabled: bool = True
  tech_enabled: bool = True
  project_enabled: bool = True
  hr_enabled: bool = True
  planned_duration: Optional[int] = None
  llm_provider: Optional[str] = None
  custom_jd_text: Optional[str] = None


class VoiceSessionResponseDTO(BaseModel):
  session_id: int
  status: str
  current_phase: Optional[str] = None
  planned_duration: Optional[int] = None
  web_socket_url: Optional[str] = None


class VoiceSessionMetaDTO(BaseModel):
  session_id: int
  role_type: str
  skill_id: Optional[str] = None
  difficulty: Optional[str] = None
  status: Optional[str] = None
  current_phase: Optional[str] = None
  planned_duration: Optional[int] = None
  actual_duration: Optional[int] = None
  start_time: Optional[str] = None
  end_time: Optional[str] = None
  evaluate_status: Optional[str] = None
  evaluate_error: Optional[str] = None


class VoiceInterviewMessageDTO(BaseModel):
  id: int
  session_id: int
  message_type: str
  phase: Optional[str] = None
  user_recognized_text: str = ""
  ai_generated_text: str = ""
  timestamp: Optional[str] = None
  sequence_num: Optional[int] = None


class WebSocketControlMessage(BaseModel):
  type: str
  session_id: Optional[int] = None


class WebSocketSubtitleMessage(BaseModel):
  session_id: int
  text: str
  is_final: bool = False
