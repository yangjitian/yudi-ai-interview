"""Read-only probe for LLM provider API keys encrypted with the dev fallback key."""

import asyncio

from sqlalchemy import select

from app.config.database import _async_session_factory
from app.config.settings import get_settings
from app.infrastructure.ai.api_key_encryption import (
    DEV_FALLBACK_KEY,
    ApiKeyEncryptionService,
)
from app.models.llm_provider import LlmProviderEntity


def _service_for_key(key: str) -> ApiKeyEncryptionService:
    previous_instance = ApiKeyEncryptionService._instance
    try:
        ApiKeyEncryptionService._instance = None
        return ApiKeyEncryptionService(key)
    finally:
        ApiKeyEncryptionService._instance = previous_instance


def _try_decrypt(service: ApiKeyEncryptionService, provider: LlmProviderEntity) -> str | None:
    try:
        return service.decrypt(provider.api_key_nonce, provider.api_key_ciphertext)
    except Exception:
        return None


async def main() -> None:
    settings = get_settings()
    current_key = settings.app_ai_config_encryption_key or DEV_FALLBACK_KEY
    current_service = _service_for_key(current_key)
    fallback_service = _service_for_key(DEV_FALLBACK_KEY)

    async with _async_session_factory() as session:
        result = await session.execute(
            select(LlmProviderEntity).order_by(LlmProviderEntity.created_at, LlmProviderEntity.id)
        )
        providers = list(result.scalars().all())

    print("provider_id|builtin|enabled|created_at|updated_at|current_key|fallback_key|status")
    normal_count = 0
    fallback_count = 0
    undecryptable_count = 0
    ambiguous_count = 0

    for provider in providers:
        current_ok = _try_decrypt(current_service, provider) is not None
        fallback_ok = _try_decrypt(fallback_service, provider) is not None
        if current_ok and not fallback_ok:
            status = "current_key"
            normal_count += 1
        elif fallback_ok and not current_ok:
            status = "fallback_only"
            fallback_count += 1
        elif current_ok and fallback_ok:
            status = "both_keys"
            ambiguous_count += 1
        else:
            status = "undecryptable"
            undecryptable_count += 1

        print(
            "|".join(
                str(value)
                for value in (
                    provider.id,
                    provider.builtin,
                    provider.enabled,
                    provider.created_at,
                    provider.updated_at,
                    current_ok,
                    fallback_ok,
                    status,
                )
            )
        )

    print(
        f"summary: current_key={normal_count} fallback_only={fallback_count} "
        f"undecryptable={undecryptable_count} both_keys={ambiguous_count} "
        f"total={len(providers)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
