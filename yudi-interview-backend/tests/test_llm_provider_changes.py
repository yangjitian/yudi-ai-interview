import unittest
from unittest.mock import AsyncMock, patch

from app.core.errors import BusinessException, ErrorCode
from app.infrastructure.ai.api_key_encryption import get_encryption_service
from app.infrastructure.redis.vectorize_producer import VectorizeStreamConsumer
from app.models.llm_provider import LlmGlobalSettingEntity, LlmProviderEntity
from app.services.llm.admin import LlmProviderAdminService


class LlmProviderModelTest(unittest.TestCase):
  def test_models_use_java_table_structure(self) -> None:
    provider_columns = LlmProviderEntity.__table__.columns
    setting_columns = LlmGlobalSettingEntity.__table__.columns

    self.assertEqual("llm_provider_config", LlmProviderEntity.__tablename__)
    self.assertEqual(64, provider_columns["id"].type.length)
    self.assertEqual(4096, provider_columns["api_key_ciphertext"].type.length)
    self.assertFalse(provider_columns["api_key_nonce"].nullable)
    self.assertFalse(provider_columns["enabled"].nullable)
    self.assertFalse(provider_columns["builtin"].nullable)
    self.assertNotIn("is_enabled", provider_columns)

    self.assertEqual("llm_global_setting", LlmGlobalSettingEntity.__tablename__)
    self.assertIn("created_at", setting_columns)
    self.assertNotIn("embedding_dimensions", setting_columns)
    self.assertFalse(setting_columns["default_chat_provider_id"].nullable)
    self.assertFalse(setting_columns["default_embedding_provider_id"].nullable)


class ApiKeyHandlingTest(unittest.TestCase):
  def test_decryption_failure_raises_business_exception(self) -> None:
    entity = LlmProviderEntity(
        id="test",
        base_url="https://example.com/v1",
        api_key_nonce="invalid",
        api_key_ciphertext="invalid",
        model="test-model",
        embedding_model=None,
        embedding_dimensions=None,
        supports_embedding=False,
        temperature=None,
        enabled=True,
        builtin=False,
    )

    with self.assertRaises(BusinessException) as context:
      LlmProviderAdminService(None)._decrypt_api_key(entity)

    self.assertEqual(ErrorCode.PROVIDER_CONFIG_READ_FAILED.code, context.exception.code)

  def test_encryption_round_trip(self) -> None:
    service = get_encryption_service()
    encrypted = service.encrypt("provider-key")
    self.assertEqual(
        "provider-key",
        service.decrypt(encrypted.nonce, encrypted.ciphertext),
    )


class VectorizeEmbeddingTest(unittest.IsolatedAsyncioTestCase):
  async def test_generate_embeddings_uses_embedding_client(self) -> None:
    expected = [[0.1, 0.2], [0.3, 0.4]]
    with patch(
        "app.infrastructure.ai.embedding_client.EmbeddingClient.embed_batch",
        new=AsyncMock(return_value=expected),
    ) as embed_batch:
      result = await VectorizeStreamConsumer()._generate_embeddings(["a", "b"])

    self.assertEqual(expected, result)
    embed_batch.assert_awaited_once_with(["a", "b"])


if __name__ == "__main__":
  unittest.main()
