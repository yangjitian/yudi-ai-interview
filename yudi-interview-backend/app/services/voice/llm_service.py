"""
语音面试 LLM 服务。

对应 Java DashscopeLlmService（249 行），提供两个核心方法：
- chat(): 同步调用 LLM，返回优化后的语音文本
- chat_stream_sentences(): 流式调用 LLM，逐 token 检测终止标点实现分句

分句逻辑：
- 终止标点集合：。！？；!?;.
- LLM 流式输出期间逐 token 检测终止标点
- 检测到后截取 last_sentence_end → 当前位置作为一句，触发 on_sentence 回调
- 流结束后发送剩余文本（可能不以终止标点结尾）
"""
import inspect
import logging
import re
from collections.abc import Callable

import httpx

from app.config.settings import get_settings
from app.infrastructure.ai.prompt_security import sanitize, wrap_with_delimiters
from app.infrastructure.ai.provider_registry import (
    get_direct_client_with_model,
    get_voice_chat_client,
)
from app.services.voice.agent import VoiceInterviewAgent

log = logging.getLogger(__name__)
settings = get_settings()

# 终止标点集合（对应 Java DashscopeLlmService.TERMINAL_PUNCTUATION）
_TERMINAL_PUNCTUATION = frozenset("。！？；!?;.")

# Markdown 清理正则
_MD_BOLD = re.compile(r"\*\*")
_MD_CODE_BLOCK = re.compile(r"```")
_MD_CODE_INLINE = re.compile(r"`")
_MD_LIST_ITEM = re.compile(r"(?m)^\s*[-*+]\s*")
_MD_WHITESPACE = re.compile(r"\s+")


async def _maybe_await(fn: Callable, *args) -> None:
  """兼容同步和异步回调：如果回调返回 coroutine 则 await。"""
  if fn is None:
    return
  result = fn(*args)
  if inspect.iscoroutine(result):
    await result


def _has_terminal_punct(token: str) -> bool:
  """检测 token 中是否包含终止标点。"""
  return any(ch in _TERMINAL_PUNCTUATION for ch in token)


def _normalize_realtime_text(text: str) -> str:
  """清理 Markdown 格式，对应 Java normalizeRealtimeText。"""
  if not text:
    return ""
  text = _MD_BOLD.sub("", text)
  text = _MD_CODE_BLOCK.sub("", text)
  text = _MD_CODE_INLINE.sub("", text)
  text = _MD_LIST_ITEM.sub("", text)
  text = _MD_WHITESPACE.sub(" ", text)
  return text.strip()


def _optimize_for_voice(content: str) -> str:
  """优化 LLM 输出为语音友好的文本，对应 Java optimizeForVoice。"""
  normalized = _normalize_realtime_text(content)
  if not normalized:
    return "请继续。"

  max_chars = settings.voice_interview.app_voice_ai_question_max_chars
  if len(normalized) <= max_chars:
    return normalized

  truncated = normalized[:max_chars]
  last_terminal = -1
  for i in range(len(truncated) - 1, -1, -1):
    if truncated[i] in _TERMINAL_PUNCTUATION:
      last_terminal = i
      break

  if last_terminal >= max_chars // 2:
    return truncated[: last_terminal + 1]
  return truncated + "…"


def _build_messages(
    user_input: str,
    session_entity,
    conversation_history: list[str] | None,
    system_prompt: str,
) -> list[dict[str, str]]:
  """构建 OpenAI 兼容格式的消息列表，对应 Java buildPromptContext。"""
  messages = [{"role": "system", "content": system_prompt}]

  if conversation_history:
    history_text = "\n".join(conversation_history)
    messages.append({
        "role": "user",
        "content": f"【之前的对话】\n{history_text}\n\n【当前对话】\n用户：{wrap_with_delimiters(sanitize(user_input))}",
    })
  else:
    messages.append({
        "role": "user",
        "content": f"用户：{wrap_with_delimiters(sanitize(user_input))}",
    })
  return messages


class LlmService:
  """基于 LangChain ChatOpenAI 的 LLM 服务。

  适用于对延迟不敏感的场景。如需低延迟流式，使用 LlmServiceDirect。
  """

  async def chat(
      self,
      user_input: str,
      session_entity,
      conversation_history: list[str] | None = None,
  ) -> str:
    agent = _build_agent(session_entity)
    system_prompt = agent.build_voice_system_prompt()
    messages = _build_messages(user_input, session_entity, conversation_history, system_prompt)

    try:
      chat_client = await get_voice_chat_client(session_entity.llm_provider)
      from langchain_core.messages import HumanMessage, SystemMessage

      lc_messages = [SystemMessage(content=system_prompt)]
      for msg in messages[1:]:
        lc_messages.append(HumanMessage(content=msg["content"]))

      response = await chat_client.ainvoke(lc_messages)
      content = response.content if hasattr(response, "content") else str(response)
      return _optimize_for_voice(content)
    except Exception as e:
      log.error("LlmService.chat error: %s", e)
      return _map_error_to_message(e)

  async def chat_stream_sentences(
      self,
      user_input: str,
      on_token: Callable[[str], None] | None = None,
      on_sentence: Callable[[str], None] | None = None,
      session_entity=None,
      conversation_history: list[str] | None = None,
  ) -> str:
    """LangChain 版本的流式分句。

    对于低延迟场景建议使用 LlmServiceDirect.chat_stream_sentences。
    """
    agent = _build_agent(session_entity)
    system_prompt = agent.build_voice_system_prompt()
    messages = _build_messages(user_input, session_entity, conversation_history, system_prompt)

    try:
      chat_client = await get_voice_chat_client(session_entity.llm_provider)
      from langchain_core.messages import HumanMessage, SystemMessage

      lc_messages = [SystemMessage(content=system_prompt)]
      for msg in messages[1:]:
        lc_messages.append(HumanMessage(content=msg["content"]))

      raw_parts: list[str] = []
      last_sentence_end = 0

      async for chunk in chat_client.astream(lc_messages):
        token = chunk.content if hasattr(chunk, "content") else str(chunk)
        if not token:
          continue
        raw_parts.append(token)
        full_text = "".join(raw_parts)
        normalized = _normalize_realtime_text(full_text)

        if on_token is not None:
          await _maybe_await(on_token, normalized)

        if on_sentence is not None and _has_terminal_punct(token):
          current_end = len(normalized)
          if current_end > last_sentence_end:
            sentence = normalized[last_sentence_end:].strip()
            if sentence:
              await _maybe_await(on_sentence, sentence)
            last_sentence_end = current_end

      # 发送剩余文本
      final_text = _normalize_realtime_text("".join(raw_parts))
      if on_sentence is not None and len(final_text) > last_sentence_end:
        remaining = final_text[last_sentence_end:].strip()
        if remaining:
          await _maybe_await(on_sentence, remaining)

      return _optimize_for_voice("".join(raw_parts))
    except Exception as e:
      log.error("LlmService.chat_stream_sentences error: %s", e)
      return _map_error_to_message(e)


class LlmServiceDirect:
  """基于 httpx 直连 DashScope 的低延迟 LLM 服务。

  绕过 LangChain 层，直接调用 DashScope OpenAI 兼容 API。
  适用于语音面试等对首字延迟敏感的场景。
  """

  async def chat(
      self,
      user_input: str,
      session_entity,
      conversation_history: list[str] | None = None,
  ) -> str:
    agent = _build_agent(session_entity)
    system_prompt = agent.build_voice_system_prompt()
    messages = _build_messages(user_input, session_entity, conversation_history, system_prompt)

    try:
      http_client, model = await get_direct_client_with_model(
          session_entity.llm_provider
      )
      response = await http_client.post(
          "/chat/completions",
          json={
              "model": model,
              "messages": messages,
              "stream": False,
              "temperature": 0.7,
          },
      )
      response.raise_for_status()
      data = response.json()
      content = data["choices"][0]["message"]["content"]
      return _optimize_for_voice(content)
    except Exception as e:
      log.error("LlmServiceDirect.chat error: %s", e)
      return _map_error_to_message(e)

  async def chat_stream_sentences(
      self,
      user_input: str,
      on_token: Callable[[str], None] | None = None,
      on_sentence: Callable[[str], None] | None = None,
      session_entity=None,
      conversation_history: list[str] | None = None,
  ) -> str:
    """流式调用 LLM，逐 token 检测终止标点实现分句。

    对应 Java DashscopeLlmService.chatStreamSentences。
    - on_token(partial_text): 每次有新 token 时触发，传入累计归一化文本
    - on_sentence(sentence): 检测到完整句子时触发
    """
    agent = _build_agent(session_entity)
    system_prompt = agent.build_voice_system_prompt()
    messages = _build_messages(user_input, session_entity, conversation_history, system_prompt)

    try:
      http_client, model = await get_direct_client_with_model(
          session_entity.llm_provider
      )

      raw_parts: list[str] = []
      last_sentence_end = 0

      async with http_client.stream(
          "POST",
          "/chat/completions",
          json={
              "model": model,
              "messages": messages,
              "stream": True,
              "temperature": 0.7,
          },
      ) as response:
        response.raise_for_status()
        async for token in _parse_sse_stream(response):
          if not token:
            continue
          raw_parts.append(token)
          full_text = "".join(raw_parts)
          normalized = _normalize_realtime_text(full_text)

          # 实时文本推送
          if on_token is not None:
            await _maybe_await(on_token, normalized)

          # 检测句子边界，回调 on_sentence
          if on_sentence is not None and _has_terminal_punct(token):
            current_end = len(normalized)
            if current_end > last_sentence_end:
              sentence = normalized[last_sentence_end:].strip()
              if sentence:
                await _maybe_await(on_sentence, sentence)
              last_sentence_end = current_end

      # 发送最后一段（可能不以终止标点结尾）
      final_text = _normalize_realtime_text("".join(raw_parts))
      if on_sentence is not None and len(final_text) > last_sentence_end:
        remaining = final_text[last_sentence_end:].strip()
        if remaining:
          await _maybe_await(on_sentence, remaining)

      optimized = _optimize_for_voice("".join(raw_parts))
      if on_token is not None and optimized:
        await _maybe_await(on_token, optimized)

      return optimized

    except Exception as e:
      log.error("LlmServiceDirect.chat_stream_sentences error: %s", e)
      return _map_error_to_message(e)


# ==================== Private Helpers ====================


def _build_agent(session_entity) -> VoiceInterviewAgent:
  """从 session entity 构建 VoiceInterviewAgent 用于获取 system prompt。"""
  return VoiceInterviewAgent(
      skill_id=session_entity.skill_id or "java-backend",
      difficulty=session_entity.difficulty or "mid",
      planned_duration=session_entity.planned_duration or 25,
      llm_provider=session_entity.llm_provider,
      tech_enabled=bool(session_entity.tech_enabled),
      project_enabled=bool(session_entity.project_enabled),
      hr_enabled=bool(session_entity.hr_enabled),
      resume_text=None,
  )


def _map_error_to_message(error: Exception) -> str:
  """将 LLM 错误映射为用户友好的提示，对应 Java mapLlmErrorToUserMessage。"""
  msg = str(error)
  if "403" in msg or "ACCESS_DENIED" in msg or "Authentication" in msg:
    return "AI 服务认证失败，请检查 API Key 配置"
  if "timeout" in msg.lower():
    return "AI 服务响应超时，请稍后重试"
  if "429" in msg or "rate limit" in msg.lower() or "quota" in msg:
    return "AI 服务调用频率超限，请稍后重试"
  if "connection" in msg.lower() or "network" in msg.lower():
    return "AI 服务网络连接失败，请检查网络"
  return "抱歉，AI 服务暂时不可用，请稍后重试"


async def _parse_sse_stream(response: httpx.Response):
  """解析 OpenAI 兼容 SSE 流，逐行 yield token 内容。

  处理格式：
    data: {"choices":[{"delta":{"content":"token"}}]}
    data: [DONE]
  """
  import json

  buffer = ""
  async for raw_bytes in response.aiter_bytes():
    buffer += raw_bytes.decode("utf-8", errors="replace")
    while "\n" in buffer:
      line, buffer = buffer.split("\n", 1)
      line = line.strip()
      if not line or not line.startswith("data:"):
        continue
      data_str = line[5:].strip()
      if data_str == "[DONE]":
        return
      try:
        data = json.loads(data_str)
        choices = data.get("choices", [])
        if not choices:
          continue
        delta = choices[0].get("delta", {})
        content = delta.get("content", "")
        if content:
          yield content
      except (json.JSONDecodeError, KeyError, IndexError):
        continue
