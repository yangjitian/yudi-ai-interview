from pathlib import Path
import json
from functools import lru_cache

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent.parent
ENV_FILE = BASE_DIR / ".env"


def _detect_env_encoding():
  if not ENV_FILE.exists():
    return "utf-8"
  try:
    ENV_FILE.read_text(encoding="utf-8")
    return "utf-8"
  except UnicodeDecodeError:
    return "gbk"


ENV_FILE_ENCODING = _detect_env_encoding()


def _parse_cors_origins(v):
  if isinstance(v, list):
    return v
  if isinstance(v, str):
    if not v.strip():
      return []
    try:
      return json.loads(v)
    except Exception:
      return [o.strip() for o in v.split(",") if o.strip()]
  return v


class DatabaseSettings(BaseModel):
  model_config = ConfigDict(populate_by_name=True)

  url: str | None = Field(default=None, alias="DB_URL")
  host: str = Field(default="localhost", alias="DB_HOST")
  port: int = Field(default=5432, alias="DB_PORT")
  username: str = Field(default="postgres", alias="DB_USERNAME")
  password: str = Field(default="password", alias="DB_PASSWORD")
  name: str = Field(default="yudi_interview", alias="DB_NAME")

  @property
  def connection_url(self) -> str:
    if self.url:
      return self.url
    from urllib.parse import quote
    encoded_password = quote(self.password, safe='')
    return (
      f"postgresql+asyncpg://{self.username}:{encoded_password}"
      f"@{self.host}:{self.port}/{self.name}"
    )

  @property
  def url(self) -> str:
    return self.connection_url

  @property
  def sync_url(self) -> str:
    from urllib.parse import quote
    encoded_password = quote(self.password, safe='')
    return (
      f"postgresql+psycopg2://{self.username}:{encoded_password}"
      f"@{self.host}:{self.port}/{self.name}"
    )


class RedisSettings(BaseModel):
  url: str | None = Field(default=None, alias="REDIS_URL")
  host: str = Field(default="localhost", alias="REDIS_HOST")
  port: int = Field(default=6379, alias="REDIS_PORT")
  password: str | None = Field(default=None, alias="REDIS_PASSWORD")
  db: int = Field(default=0, alias="REDIS_DB")

  @property
  def connection_url(self) -> str:
    if self.url:
      return self.url
    from urllib.parse import quote
    if self.password:
      encoded_password = quote(self.password, safe='')
      return f"redis://:{encoded_password}@{self.host}:{self.port}/{self.db}"
    return f"redis://{self.host}:{self.port}/{self.db}"

  @property
  def url(self) -> str:
    return self.connection_url


class StorageSettings(BaseModel):
  endpoint: str = Field(default="http://localhost:9000")
  public_endpoint: str = Field(default="")
  access_key: str = Field(default="minioadmin")
  secret_key: str = Field(default="minioadmin")
  bucket: str = Field(default="interview-guide")
  region: str = Field(default="us-east-1")
  use_path_style: bool = True


class AIProviderConfig(BaseModel):
  base_url: str = ""
  api_key: str = ""
  model: str = ""
  embedding_model: str | None = None
  embedding_dimensions: int | None = None
  temperature: float | None = None
  supports_embedding: bool = False


class AISettings(BaseSettings):
  model_config = SettingsConfigDict(
      env_file=ENV_FILE,
      env_file_encoding=ENV_FILE_ENCODING,
      extra="ignore",
  )
  bailian_api_key: str = Field(default="", alias="AI_BAILIAN_API_KEY")
  model: str = Field(default="qwen3.6-flash", alias="AI_MODEL")
  default_provider: str = Field(default="dashscope", alias="AI_DEFAULT_PROVIDER")
  default_embedding_provider: str = Field(default="dashscope", alias="AI_DEFAULT_EMBEDDING_PROVIDER")
  provider_dashscope_base_url: str = Field(
      default="https://dashscope.aliyuncs.com/compatible-mode/v1",
      alias="AI_PROVIDER_DASHSCOPE_BASE_URL",
  )
  provider_dashscope_model: str = Field(
      default="qwen3.5-flash",
      validation_alias=AliasChoices("AI_PROVIDER_DASHSCOPE_MODEL", "AI_MODEL"),
  )
  provider_dashscope_embedding_model: str = Field(
      default="text-embedding-v3",
      alias="AI_PROVIDER_DASHSCOPE_EMBEDDING_MODEL",
  )
  provider_dashscope_embedding_dimensions: int = Field(default=1024, alias="AI_PROVIDER_DASHSCOPE_EMBEDDING_DIMENSIONS")
  provider_lmstudio_base_url: str = Field(default="http://localhost:1234", alias="AI_PROVIDER_LMSTUDIO_BASE_URL")
  provider_lmstudio_api_key: str = Field(
      default="lm-studio",
      validation_alias=AliasChoices("PROVIDER_LMSTUDIO_API_KEY", "AI_PROVIDER_LMSTUDIO_API_KEY"),
  )
  provider_lmstudio_model: str = Field(default="qwen2.5-7b-instruct", alias="AI_PROVIDER_LMSTUDIO_MODEL")
  provider_kimi_api_key: str = Field(default="", alias="PROVIDER_KIMI_API_KEY")
  provider_kimi_model: str = Field(default="kimi-latest", alias="PROVIDER_KIMI_MODEL")
  provider_deepseek_api_key: str = Field(default="", alias="PROVIDER_DEEPSEEK_API_KEY")
  provider_deepseek_model: str = Field(default="deepseek-v4-flash", alias="PROVIDER_DEEPSEEK_MODEL")
  provider_glm_api_key: str = Field(default="", alias="PROVIDER_GLM_API_KEY")
  provider_glm_model: str = Field(default="glm-5", alias="PROVIDER_GLM_MODEL")

  # Structured output 重试配置（与 Java StructuredOutputProperties 对齐）
  structured_max_attempts: int = Field(default=2, alias="APP_AI_STRUCTURED_MAX_ATTEMPTS")
  structured_include_last_error: bool = Field(default=True, alias="APP_AI_STRUCTURED_INCLUDE_LAST_ERROR")
  structured_retry_use_repair_prompt: bool = Field(default=True, alias="APP_AI_STRUCTURED_RETRY_USE_REPAIR_PROMPT")
  structured_retry_append_strict_json_instruction: bool = Field(default=True, alias="APP_AI_STRUCTURED_RETRY_APPEND_STRICT_JSON_INSTRUCTION")
  structured_error_message_max_length: int = Field(default=200, alias="APP_AI_STRUCTURED_ERROR_MESSAGE_MAX_LENGTH")
  structured_metrics_enabled: bool = Field(default=True, alias="APP_AI_STRUCTURED_METRICS_ENABLED")

  @property
  def providers(self) -> dict[str, AIProviderConfig]:
    return {
      "dashscope": AIProviderConfig(
          base_url=self.provider_dashscope_base_url,
          api_key=self.bailian_api_key,
          model=self.provider_dashscope_model,
          embedding_model=self.provider_dashscope_embedding_model,
          embedding_dimensions=self.provider_dashscope_embedding_dimensions,
          supports_embedding=True,
      ),
      "lmstudio": AIProviderConfig(
          base_url=self.provider_lmstudio_base_url,
          api_key=self.provider_lmstudio_api_key,
          model=self.provider_lmstudio_model,
          supports_embedding=False,
      ),
      "kimi": AIProviderConfig(
          base_url="https://api.moonshot.cn/v1",
          api_key=self.provider_kimi_api_key,
          model=self.provider_kimi_model,
          temperature=1,
          supports_embedding=False,
      ),
      "deepseek": AIProviderConfig(
          base_url="https://api.deepseek.com",
          api_key=self.provider_deepseek_api_key,
          model=self.provider_deepseek_model,
          supports_embedding=False,
      ),
      "glm": AIProviderConfig(
          base_url="https://open.bigmodel.cn/api/coding/paas/v4",
          api_key=self.provider_glm_api_key,
          model=self.provider_glm_model,
          embedding_model="embedding-3",
          embedding_dimensions=1024,
          supports_embedding=True,
      ),
    }


class VoicePhaseDurationConfig(BaseModel):
  min_duration: int
  suggested_duration: int
  max_duration: int
  min_questions: int
  max_questions: int


class VoicePhaseConfig(BaseModel):
  intro: VoicePhaseDurationConfig = Field(
      default_factory=lambda: VoicePhaseDurationConfig(
          min_duration=3,
          suggested_duration=5,
          max_duration=8,
          min_questions=2,
          max_questions=5,
      )
  )
  tech: VoicePhaseDurationConfig = Field(
      default_factory=lambda: VoicePhaseDurationConfig(
          min_duration=8,
          suggested_duration=10,
          max_duration=15,
          min_questions=3,
          max_questions=8,
      )
  )
  project: VoicePhaseDurationConfig = Field(
      default_factory=lambda: VoicePhaseDurationConfig(
          min_duration=8,
          suggested_duration=10,
          max_duration=15,
          min_questions=2,
          max_questions=5,
      )
  )
  hr: VoicePhaseDurationConfig = Field(
      default_factory=lambda: VoicePhaseDurationConfig(
          min_duration=3,
          suggested_duration=5,
          max_duration=8,
          min_questions=2,
          max_questions=5,
      )
  )


class VoiceInterviewSettings(BaseSettings):
  model_config = SettingsConfigDict(
      env_file=ENV_FILE,
      env_file_encoding=ENV_FILE_ENCODING,
      extra="ignore",
  )
  app_voice_asr_url: str = Field(default="", alias="APP_VOICE_ASR_URL")
  app_voice_asr_model: str = Field(default="", alias="APP_VOICE_ASR_MODEL")
  app_voice_asr_language: str = Field(default="zh", alias="APP_VOICE_ASR_LANGUAGE")
  app_voice_asr_format: str = Field(default="pcm", alias="APP_VOICE_ASR_FORMAT")
  app_voice_asr_sample_rate: int = Field(default=16000, alias="APP_VOICE_ASR_SAMPLE_RATE")
  app_voice_asr_enable_turn_detection: bool = Field(default=True, alias="APP_VOICE_ASR_ENABLE_TURN_DETECTION")
  app_voice_asr_turn_detection_type: str = Field(default="server_vad", alias="APP_VOICE_ASR_TURN_DETECTION_TYPE")
  app_voice_asr_turn_detection_threshold: float = Field(default=0.0, alias="APP_VOICE_ASR_TURN_DETECTION_THRESHOLD")
  app_voice_asr_silence_ms: int = Field(default=2000, alias="APP_VOICE_ASR_SILENCE_MS")
  app_voice_asr_chunk_ms: int = Field(default=200, alias="APP_VOICE_ASR_CHUNK_MS")
  app_voice_asr_filter_filler_words: bool = Field(
      default=True, alias="APP_VOICE_ASR_FILTER_FILLER_WORDS"
  )
  app_voice_tts_model: str = Field(default="qwen3-tts-flash-realtime", alias="APP_VOICE_TTS_MODEL")
  app_voice_tts_api_key: str = Field(default="", alias="APP_VOICE_TTS_API_KEY")
  app_voice_tts_voice: str = Field(default="Cherry", alias="APP_VOICE_TTS_VOICE")
  app_voice_tts_format: str = Field(default="pcm", alias="APP_VOICE_TTS_FORMAT")
  app_voice_tts_sample_rate: int = Field(default=24000, alias="APP_VOICE_TTS_SAMPLE_RATE")
  app_voice_tts_mode: str = Field(default="commit", alias="APP_VOICE_TTS_MODE")
  app_voice_tts_language_type: str = Field(default="Chinese", alias="APP_VOICE_TTS_LANGUAGE_TYPE")
  app_voice_tts_speech_rate: float = Field(default=1.0, alias="APP_VOICE_TTS_SPEECH_RATE")
  app_voice_tts_volume: int = Field(default=60, alias="APP_VOICE_TTS_VOLUME")
  app_voice_user_utterance_debounce_ms: int = Field(default=2500, alias="APP_VOICE_USER_UTTERANCE_DEBOUNCE_MS")
  app_voice_min_silence_before_commit_ms: int = Field(default=2500, alias="APP_VOICE_MIN_SILENCE_BEFORE_COMMIT_MS")
  app_voice_min_commit_chars: int = Field(default=20, alias="APP_VOICE_MIN_COMMIT_CHARS")
  app_voice_max_wait_for_continuation_ms: int = Field(default=7000, alias="APP_VOICE_MAX_WAIT_FOR_CONTINUATION_MS")
  app_voice_ai_question_max_chars: int = Field(default=120, alias="APP_VOICE_AI_QUESTION_MAX_CHARS")
  app_voice_chunked_audio_enabled: bool = Field(default=True, alias="APP_VOICE_CHUNKED_AUDIO_ENABLED")
  app_voice_tts_timeout_seconds: int = Field(default=8, alias="APP_VOICE_TTS_TIMEOUT_SECONDS")
  app_voice_max_concurrent_tts_per_session: int = Field(default=3, alias="APP_VOICE_MAX_CONCURRENT_TTS_PER_SESSION")
  app_voice_opening_audio_warmup_enabled: bool = Field(
      default=False, alias="APP_VOICE_OPENING_AUDIO_WARMUP_ENABLED"
  )
  app_voice_tts_pool_idle_timeout_seconds: int = Field(
      default=300, alias="APP_VOICE_TTS_POOL_IDLE_TIMEOUT_SECONDS"
  )
  max_concurrent_tts_per_session: int = Field(default=3)
  phase: VoicePhaseConfig = Field(default_factory=VoicePhaseConfig)

  # asr_service.py 使用的短名别名（与 app_voice_asr_* 共存）
  # asr_service.py 中使用 self._cfg.asr_* 访问，这里用别名指向 app_voice_asr_* 字段
  asr_url: str = Field(default="")
  asr_model: str = Field(default="")
  asr_language: str = Field(default="zh")
  asr_format: str = Field(default="pcm")
  asr_sample_rate: int = Field(default=16000)
  asr_enable_turn_detection: bool = Field(default=True)
  asr_turn_detection_type: str = Field(default="server_vad")
  asr_turn_detection_threshold: float = Field(default=0.0)
  asr_turn_detection_silence_duration_ms: int = Field(default=2000)
  asr_audio_chunk_ms: int = Field(default=200)
  asr_filter_filler_words: bool = Field(default=True)
  asr_api_key: str = Field(default="")

  # use_direct_llm_client 配置（ws_handler.py _init_llm_client 使用）
  use_direct_llm_client: bool = Field(default=False, alias="APP_VOICE_USE_DIRECT_LLM")

  # tts_service.py 使用的短名别名（tts_* 指向 app_voice_tts_*）
  tts_model: str = Field(default="")
  tts_voice: str = Field(default="")
  tts_format: str = Field(default="")
  tts_sample_rate: int = Field(default=24000)
  tts_mode: str = Field(default="commit")
  tts_language_type: str = Field(default="Chinese")
  tts_speech_rate: float = Field(default=1.0)
  tts_volume: int = Field(default=60)
  tts_timeout_seconds: int = Field(default=30)
  tts_api_key: str = Field(default="")
  tts_url: str = Field(default="")

  @property
  def _tts_pool_size(self) -> int:
    return self.app_voice_max_concurrent_tts_per_session

  @model_validator(mode="after")
  def _sync_tts_pool_size(self) -> "VoiceInterviewSettings":
        self.max_concurrent_tts_per_session = self.app_voice_max_concurrent_tts_per_session
        # 同步 asr_service.py 使用的短名到 app_voice_asr_* 值
        self.asr_url = self.app_voice_asr_url
        self.asr_model = self.app_voice_asr_model
        self.asr_language = self.app_voice_asr_language
        self.asr_format = self.app_voice_asr_format
        self.asr_sample_rate = self.app_voice_asr_sample_rate
        self.asr_enable_turn_detection = self.app_voice_asr_enable_turn_detection
        self.asr_turn_detection_type = self.app_voice_asr_turn_detection_type
        self.asr_turn_detection_threshold = self.app_voice_asr_turn_detection_threshold
        self.asr_turn_detection_silence_duration_ms = self.app_voice_asr_silence_ms
        self.asr_audio_chunk_ms = self.app_voice_asr_chunk_ms
        self.asr_filter_filler_words = self.app_voice_asr_filter_filler_words
        # 同步 tts_* 短名
        self.tts_model = self.tts_model or self.app_voice_tts_model
        self.tts_voice = self.tts_voice or self.app_voice_tts_voice
        self.tts_format = self.tts_format or self.app_voice_tts_format
        self.tts_sample_rate = self.tts_sample_rate or self.app_voice_tts_sample_rate
        self.tts_mode = self.tts_mode or self.app_voice_tts_mode
        self.tts_language_type = self.tts_language_type or self.app_voice_tts_language_type
        self.tts_speech_rate = self.tts_speech_rate or self.app_voice_tts_speech_rate
        self.tts_volume = self.tts_volume or self.app_voice_tts_volume
        self.tts_timeout_seconds = self.tts_timeout_seconds or self.app_voice_tts_timeout_seconds
        self.tts_api_key = self.tts_api_key or self.app_voice_tts_api_key
        self.tts_url = self.tts_url or ""  # TTS SDK 使用默认 URL，空字符串表示使用 SDK 默认
        return self

  app_voice_rate_limit_max_per_session: int = Field(default=10, alias="APP_VOICE_RATE_LIMIT_MAX_PER_SESSION")
  app_voice_rate_limit_max_per_ip: int = Field(default=3, alias="APP_VOICE_RATE_LIMIT_MAX_PER_IP")
  app_voice_rate_limit_max_concurrent: int = Field(default=50, alias="APP_VOICE_RATE_LIMIT_MAX_CONCURRENT")
  app_voice_audio_codec: str = Field(default="opus", alias="APP_VOICE_AUDIO_CODEC")
  app_voice_audio_sample_rate: int = Field(default=16000, alias="APP_VOICE_AUDIO_SAMPLE_RATE")
  app_voice_audio_bit_rate: int = Field(default=24000, alias="APP_VOICE_AUDIO_BIT_RATE")
  app_voice_audio_channels: int = Field(default=1, alias="APP_VOICE_AUDIO_CHANNELS")
  app_voice_audio_chunk_duration: int = Field(default=2000, alias="APP_VOICE_AUDIO_CHUNK_DURATION")


class InterviewSettings(BaseSettings):
  model_config = SettingsConfigDict(
      env_file=ENV_FILE,
      env_file_encoding=ENV_FILE_ENCODING,
      extra="ignore",
  )
  app_interview_follow_up_count: int = Field(default=1, alias="APP_INTERVIEW_FOLLOW_UP_COUNT")
  app_interview_evaluation_batch_size: int = Field(default=8, alias="APP_INTERVIEW_EVALUATION_BATCH_SIZE")
  question_generation_timeout_seconds: int = Field(default=90, alias="APP_INTERVIEW_QUESTION_GENERATION_TIMEOUT_SECONDS")
  evaluation_batch_retry_count: int = Field(default=2, alias="APP_INTERVIEW_EVALUATION_BATCH_RETRY_COUNT")
  evaluation_single_timeout: int = Field(default=95, alias="EVAL_SINGLE_TIMEOUT")
  evaluation_summary_timeout_seconds: int = Field(default=95, alias="APP_INTERVIEW_EVALUATION_SUMMARY_TIMEOUT_SECONDS")
  evaluation_strategy: str = Field(default="batch", alias="APP_INTERVIEW_EVALUATION_STRATEGY")
  evaluation_question_timeout_seconds: int = Field(default=60, alias="APP_INTERVIEW_EVALUATION_QUESTION_TIMEOUT_SECONDS")
  evaluation_question_retry_count: int = Field(default=2, alias="APP_INTERVIEW_EVALUATION_QUESTION_RETRY_COUNT")
  evaluation_question_concurrency: int = Field(default=8, alias="APP_INTERVIEW_EVALUATION_QUESTION_CONCURRENCY")
  evaluation_question_reference_max_chars: int = Field(default=1800, alias="APP_INTERVIEW_EVALUATION_QUESTION_REFERENCE_MAX_CHARS")
  evaluation_sse_heartbeat_seconds: int = Field(default=15, alias="APP_INTERVIEW_EVALUATION_SSE_HEARTBEAT_SECONDS")


class AppSettings(BaseSettings):
  model_config = SettingsConfigDict(
      env_file=ENV_FILE,
      env_file_encoding=ENV_FILE_ENCODING,
      extra="ignore",
  )
  app_debug: bool = Field(default=False, alias="APP_DEBUG")
  app_upload_dir: Path = Field(default=Path("/tmp/ai-interview/uploads"), alias="APP_UPLOAD_DIR")
  cors_origins: list[str] = Field(
      default_factory=lambda: ["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"],
      alias="CORS_ORIGINS",
  )

  @field_validator("cors_origins", mode="before")
  @classmethod
  def validate_cors_origins(cls, value):
    return _parse_cors_origins(value)


class Settings(BaseSettings):
  model_config = SettingsConfigDict(
      env_file=ENV_FILE,
      env_file_encoding=ENV_FILE_ENCODING,
      extra="ignore",
  )
  db_url: str | None = Field(default=None, alias="DB_URL")
  db_host: str = Field(default="localhost", alias="DB_HOST")
  db_port: int = Field(default=5432, alias="DB_PORT")
  db_username: str = Field(default="postgres", alias="DB_USERNAME")
  db_password: str = Field(default="password", alias="DB_PASSWORD")
  db_name: str = Field(default="yudi_interview", alias="DB_NAME")
  redis_url: str | None = Field(default=None, alias="REDIS_URL")
  redis_host: str = Field(default="localhost", alias="REDIS_HOST")
  redis_port: int = Field(default=6379, alias="REDIS_PORT")
  redis_password: str | None = Field(default=None, alias="REDIS_PASSWORD")
  redis_db: int = Field(default=0, alias="REDIS_DB")
  storage_endpoint: str = Field(default="http://localhost:9000", alias="STORAGE_ENDPOINT")
  storage_endpoint_public: str = Field(default="", alias="STORAGE_ENDPOINT_PUBLIC")
  storage_access_key: str = Field(default="minioadmin", alias="STORAGE_ACCESS_KEY")
  storage_secret_key: str = Field(default="minioadmin", alias="STORAGE_SECRET_KEY")
  storage_bucket: str = Field(default="interview-guide", alias="STORAGE_BUCKET")
  storage_region: str = Field(default="us-east-1", alias="STORAGE_REGION")
  bailian_api_key: str = Field(default="", alias="AI_BAILIAN_API_KEY")
  model: str = Field(default="qwen3.6-flash", alias="AI_MODEL")
  default_provider: str = Field(default="dashscope", alias="AI_DEFAULT_PROVIDER")
  default_embedding_provider: str = Field(default="dashscope", alias="AI_DEFAULT_EMBEDDING_PROVIDER")
  provider_dashscope_base_url: str = Field(default="", alias="AI_PROVIDER_DASHSCOPE_BASE_URL")
  provider_dashscope_model: str = Field(default="", alias="AI_PROVIDER_DASHSCOPE_MODEL")
  provider_dashscope_embedding_model: str = Field(default="", alias="AI_PROVIDER_DASHSCOPE_EMBEDDING_MODEL")
  provider_dashscope_embedding_dimensions: int = Field(default=1024, alias="AI_PROVIDER_DASHSCOPE_EMBEDDING_DIMENSIONS")
  provider_lmstudio_base_url: str = Field(default="", alias="AI_PROVIDER_LMSTUDIO_BASE_URL")
  provider_lmstudio_api_key: str = Field(default="", alias="AI_PROVIDER_LMSTUDIO_API_KEY")
  provider_lmstudio_model: str = Field(default="", alias="AI_PROVIDER_LMSTUDIO_MODEL")
  cors_origins: list[str] = Field(
      default_factory=lambda: ["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"],
      alias="CORS_ORIGINS",
  )
  app_debug: bool = Field(default=False, alias="APP_DEBUG")
  app_upload_dir: Path = Field(default=Path("/tmp/ai-interview/uploads"), alias="APP_UPLOAD_DIR")
  app_interview_follow_up_count: int = Field(default=1, alias="APP_INTERVIEW_FOLLOW_UP_COUNT")
  app_interview_evaluation_batch_size: int = Field(default=8, alias="APP_INTERVIEW_EVALUATION_BATCH_SIZE")
  question_generation_timeout_seconds: int = Field(default=90, alias="APP_INTERVIEW_QUESTION_GENERATION_TIMEOUT_SECONDS")
  evaluation_batch_retry_count: int = Field(default=2, alias="APP_INTERVIEW_EVALUATION_BATCH_RETRY_COUNT")
  evaluation_summary_timeout_seconds: int = Field(default=95, alias="APP_INTERVIEW_EVALUATION_SUMMARY_TIMEOUT_SECONDS")
  evaluation_strategy: str = Field(default="batch", alias="APP_INTERVIEW_EVALUATION_STRATEGY")
  evaluation_question_timeout_seconds: int = Field(default=60, alias="APP_INTERVIEW_EVALUATION_QUESTION_TIMEOUT_SECONDS")
  evaluation_question_retry_count: int = Field(default=2, alias="APP_INTERVIEW_EVALUATION_QUESTION_RETRY_COUNT")
  evaluation_question_concurrency: int = Field(default=8, alias="APP_INTERVIEW_EVALUATION_QUESTION_CONCURRENCY")
  evaluation_question_reference_max_chars: int = Field(default=1800, alias="APP_INTERVIEW_EVALUATION_QUESTION_REFERENCE_MAX_CHARS")
  evaluation_sse_heartbeat_seconds: int = Field(default=15, alias="APP_INTERVIEW_EVALUATION_SSE_HEARTBEAT_SECONDS")
  app_ai_config_encryption_key: str = Field(default="", alias="APP_AI_CONFIG_ENCRYPTION_KEY")

  @field_validator("cors_origins", mode="before")
  @classmethod
  def validate_cors_origins(cls, value):
    return _parse_cors_origins(value)

  @property
  def app(self) -> AppSettings:
    return AppSettings()

  @property
  def database(self) -> DatabaseSettings:
    return DatabaseSettings(
        url=self.db_url,
        host=self.db_host,
        port=self.db_port,
        username=self.db_username,
        password=self.db_password,
        name=self.db_name,
    )

  @property
  def redis(self) -> RedisSettings:
    return RedisSettings(
        url=self.redis_url,
        host=self.redis_host,
        port=self.redis_port,
        password=self.redis_password,
        db=self.redis_db,
    )

  @property
  def storage(self) -> StorageSettings:
    return StorageSettings(
        endpoint=self.storage_endpoint,
        public_endpoint=self.storage_endpoint_public,
        access_key=self.storage_access_key,
        secret_key=self.storage_secret_key,
        bucket=self.storage_bucket,
        region=self.storage_region,
    )

  @property
  def ai(self) -> AISettings:
    return AISettings()

  @property
  def voice_interview(self) -> VoiceInterviewSettings:
    return VoiceInterviewSettings()

  @property
  def interview(self) -> InterviewSettings:
    return InterviewSettings()


@lru_cache
def get_settings() -> Settings:
  return Settings()
