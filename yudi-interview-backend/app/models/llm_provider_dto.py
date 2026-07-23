from typing import Optional

from pydantic import BaseModel


class CreateProviderRequest(BaseModel):
  id: str
  base_url: str
  api_key: str
  model: str
  embedding_model: Optional[str] = None
  embedding_dimensions: Optional[int] = None
  supports_embedding: bool = False
  temperature: Optional[float] = None
  is_enabled: bool = True


class UpdateProviderRequest(BaseModel):
  base_url: Optional[str] = None
  api_key: Optional[str] = None
  model: Optional[str] = None
  embedding_model: Optional[str] = None
  embedding_dimensions: Optional[int] = None
  supports_embedding: Optional[bool] = None
  temperature: Optional[float] = None
  is_enabled: Optional[bool] = None


class ProviderDTO(BaseModel):
  id: str
  base_url: str
  model: str
  embedding_model: Optional[str] = None
  embedding_dimensions: Optional[int] = None
  supports_embedding: bool = False
  temperature: Optional[float] = None
  masked_api_key: str = "***"
  default_chat_provider: bool = False
  default_embedding_provider: bool = False
  is_enabled: bool = True


class TestProviderRequest(BaseModel):
    provider_id: str
    message: str = "Hello, please respond with 'OK' if you can read this."


class TestProviderResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    model: Optional[str] = None
    latency_ms: Optional[int] = None


class GlobalSettingDTO(BaseModel):
  default_chat_provider_id: Optional[str] = None
  default_embedding_provider_id: Optional[str] = None
  embedding_dimensions: int = 1024


class UpdateGlobalSettingRequest(BaseModel):
    default_chat_provider_id: Optional[str] = None
    default_embedding_provider_id: Optional[str] = None
    embedding_dimensions: Optional[int] = None


class AsrConfigDTO(BaseModel):
    url: str = ""
    model: str = ""
    masked_api_key: str = ""
    language: str = "zh-CN"
    format: str = "pcm16"
    sample_rate: int = 16000
    enable_turn_detection: bool = True
    turn_detection_type: str = "server_vad"
    turn_detection_threshold: float = 0.5
    turn_detection_silence_duration_ms: int = 800


class TtsConfigDTO(BaseModel):
    model: str = ""
    masked_api_key: str = ""
    voice: str = "Cherry"
    format: str = "pcm"
    sample_rate: int = 24000
    mode: str = "commit"
    language_type: str = "Chinese"
    speech_rate: float = 1.0
    volume: int = 60


class UpdateAsrConfigRequest(BaseModel):
    url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    language: Optional[str] = None
    format: Optional[str] = None
    sample_rate: Optional[int] = None
    enable_turn_detection: Optional[bool] = None
    turn_detection_type: Optional[str] = None
    turn_detection_threshold: Optional[float] = None
    turn_detection_silence_duration_ms: Optional[int] = None


class UpdateTtsConfigRequest(BaseModel):
    model: Optional[str] = None
    api_key: Optional[str] = None
    voice: Optional[str] = None
    format: Optional[str] = None
    sample_rate: Optional[int] = None
    mode: Optional[str] = None
    language_type: Optional[str] = None
    speech_rate: Optional[float] = None
    volume: Optional[int] = None


class VoiceConfigTestResultDTO(BaseModel):
    success: bool
    message: Optional[str] = None
    model: Optional[str] = None
