import asyncio
import gc
import logging

from app.services.kb import query as query_module
from app.services.kb.query import KnowledgeBaseQueryService, _NO_RESULT_RESPONSE


class _FakeRepository:
  async def increment_question_counts(self, knowledge_base_ids: list[int]) -> None:
    return None


class _Chunk:
  def __init__(self, content: str):
    self.content = content


async def test_no_result_pattern_closes_upstream_as_controlled_flow(
    monkeypatch,
    caplog,
) -> None:
  upstream_closed = False

  async def stream():
    nonlocal upstream_closed
    try:
      yield _Chunk("当前资料信息不足")
      yield _Chunk("后续内容不应再发送")
    finally:
      upstream_closed = True

  class _FakeChat:
    def astream(self, messages):
      return stream()

  async def fake_get_plain_chat_client(provider_id):
    return _FakeChat()

  async def fake_retrieve(query_text, knowledge_base_ids, history):
    return [{"content": "知识库内容", "score": 1.0, "source": "测试"}]

  service = KnowledgeBaseQueryService.__new__(KnowledgeBaseQueryService)
  service.kb_repo = _FakeRepository()
  service._retrieve_relevant_docs = fake_retrieve
  service._build_messages = lambda context, question, history: []
  monkeypatch.setattr(
      query_module,
      "get_plain_chat_client",
      fake_get_plain_chat_client,
  )

  with caplog.at_level(logging.INFO, logger=query_module.__name__):
    output = [
        chunk
        async for chunk in service.query_stream("测试问题", [1])
    ]

  gc.collect()
  await asyncio.sleep(0)
  await asyncio.sleep(0)

  assert output == [_NO_RESULT_RESPONSE]
  assert upstream_closed is True
  assert "按业务规则主动结束上游流" in caplog.text
  assert "pattern='信息不足'" in caplog.text
