import logging

from sqlalchemy import select

from app.config.database import _async_session_factory
from app.config.settings import get_settings
from app.infrastructure.ai.api_key_encryption import get_encryption_service
from app.models.llm_provider import LlmGlobalSettingEntity, LlmProviderEntity


log = logging.getLogger(__name__)


async def initialize_builtin_providers() -> None:
  settings = get_settings()
  provider_configs = settings.ai.providers
  encryption_service = get_encryption_service()

  async with _async_session_factory() as session:
    try:
      result = await session.execute(select(LlmProviderEntity))
      providers = {provider.id: provider for provider in result.scalars().all()}

      for provider_id, config in provider_configs.items():
        if provider_id in providers:
          continue

        encrypted = encryption_service.encrypt(config.api_key or "")
        provider = LlmProviderEntity(
            id=provider_id,
            base_url=config.base_url,
            api_key_nonce=encrypted.nonce,
            api_key_ciphertext=encrypted.ciphertext,
            model=config.model,
            embedding_model=config.embedding_model,
            embedding_dimensions=config.embedding_dimensions,
            supports_embedding=config.supports_embedding,
            temperature=config.temperature,
            enabled=True,
            builtin=True,
        )
        session.add(provider)
        providers[provider_id] = provider

      if await session.get(LlmGlobalSettingEntity, 1) is None:
        default_chat_provider_id = _resolve_default_chat_provider(
            providers,
            settings.ai.default_provider,
        )
        default_embedding_provider_id = _resolve_default_embedding_provider(
            providers,
            settings.ai.default_embedding_provider,
            default_chat_provider_id,
        )
        session.add(LlmGlobalSettingEntity(
            id=1,
            default_chat_provider_id=default_chat_provider_id,
            default_embedding_provider_id=default_embedding_provider_id,
        ))

      await session.commit()
      log.info("Builtin LLM Provider 初始化完成")
    except Exception:
      await session.rollback()
      raise


def _resolve_default_chat_provider(
    providers: dict[str, LlmProviderEntity],
    preferred_provider_id: str,
) -> str:
  if preferred_provider_id in providers:
    return preferred_provider_id
  return next(iter(providers))


def _resolve_default_embedding_provider(
    providers: dict[str, LlmProviderEntity],
    preferred_provider_id: str,
    fallback_provider_id: str,
) -> str:
  preferred = providers.get(preferred_provider_id)
  if _supports_embedding(preferred):
    return preferred_provider_id

  for provider_id, provider in providers.items():
    if _supports_embedding(provider):
      return provider_id
  return fallback_provider_id


def _supports_embedding(provider: LlmProviderEntity | None) -> bool:
  return bool(
      provider
      and provider.enabled
      and provider.supports_embedding
      and provider.embedding_model
  )
