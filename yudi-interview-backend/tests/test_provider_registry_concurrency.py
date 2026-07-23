import asyncio
import unittest
from unittest.mock import patch

from app.infrastructure.ai import provider_registry


class ProviderRegistryConcurrencyTest(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self) -> None:
    self.original_configs = provider_registry._provider_config_cache
    self.original_chat_provider = provider_registry._default_chat_provider_id
    self.original_embedding_provider = provider_registry._default_embedding_provider_id

  async def asyncTearDown(self) -> None:
    provider_registry._provider_config_cache = self.original_configs
    provider_registry._default_chat_provider_id = self.original_chat_provider
    provider_registry._default_embedding_provider_id = self.original_embedding_provider
    provider_registry._client_cache.clear()
    provider_registry._embedding_client_cache.clear()

  async def test_reader_waits_for_reload_and_reads_new_default(self) -> None:
    load_started = asyncio.Event()
    allow_load_to_finish = asyncio.Event()

    async def load_snapshot():
      load_started.set()
      await allow_load_to_finish.wait()
      return {
          "new-provider": {
              "base_url": "https://example.com/v1",
              "api_key": "key",
              "model": "chat-model",
              "embedding_model": "embedding-model",
              "embedding_dimensions": 1024,
              "supports_embedding": True,
              "temperature": 0.2,
          }
      }, "new-provider", "new-provider"

    async def read_default(_provider_id=None):
      return provider_registry._default_chat_provider_id

    with (
        patch.object(provider_registry, "_load_registry_snapshot", load_snapshot),
        patch.object(provider_registry, "_get_chat_client_locked", read_default),
    ):
      reload_task = asyncio.create_task(provider_registry.reload())
      await load_started.wait()

      reader_task = asyncio.create_task(provider_registry.get_chat_client())
      await asyncio.sleep(0)
      self.assertFalse(reader_task.done())

      allow_load_to_finish.set()
      await reload_task
      self.assertEqual("new-provider", await reader_task)


if __name__ == "__main__":
  unittest.main()
