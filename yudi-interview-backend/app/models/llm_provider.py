from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config.database import Base


class LlmProviderEntity(Base):
  __tablename__ = "llm_provider_config"

  id: Mapped[str] = mapped_column(String(64), primary_key=True)
  base_url: Mapped[str] = mapped_column(String(512), nullable=False)
  api_key_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
  api_key_ciphertext: Mapped[str] = mapped_column(String(4096), nullable=False)
  model: Mapped[str] = mapped_column(String(128), nullable=False)
  embedding_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
  embedding_dimensions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
  supports_embedding: Mapped[bool] = mapped_column(Boolean, nullable=False)
  temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
  enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
  builtin: Mapped[bool] = mapped_column(Boolean, nullable=False)
  created_at: Mapped[datetime] = mapped_column(
      DateTime(), nullable=False, server_default=func.now()
  )
  updated_at: Mapped[datetime] = mapped_column(
      DateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
  )


class LlmGlobalSettingEntity(Base):
  __tablename__ = "llm_global_setting"

  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=1)
  default_chat_provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
  default_embedding_provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
  created_at: Mapped[datetime] = mapped_column(
      DateTime(), nullable=False, server_default=func.now()
  )
  updated_at: Mapped[datetime] = mapped_column(
      DateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
  )
