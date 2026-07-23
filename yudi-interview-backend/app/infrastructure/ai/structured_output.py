import logging
import asyncio
import time
from typing import TypeVar

from pydantic import BaseModel

from app.config.settings import get_settings
from app.core.errors import BusinessException, ErrorCode


log = logging.getLogger(__name__)
settings = get_settings()


T = TypeVar("T")


def _timeout_phase(error: Exception) -> str:
  name = type(error).__name__
  if name in {"ConnectTimeout", "ConnectError"}:
    return "connect"
  if name in {"ReadTimeout", "ReadError"}:
    return "read"
  if name in {"WriteTimeout", "WriteError"}:
    return "write"
  if name in {"PoolTimeout", "PoolError"}:
    return "pool"
  if isinstance(error, asyncio.TimeoutError):
    return "outer_wait_for"
  return "unknown"


def _exception_text(error: Exception) -> str:
  message = str(error).strip()
  return f"{type(error).__name__}: {message or '(no message)'}"


class StructuredOutputInvoker:
  def __init__(
      self,
      max_retries: int | None = None,
      retry_include_error: bool | None = None,
  ):
    self.max_retries = max_retries or settings.ai.structured_max_attempts
    self.retry_include_error = (
        retry_include_error
        if retry_include_error is not None
        else settings.ai.structured_include_last_error
    )

  async def invoke(
      self,
      chat_model,
      system_prompt: str,
      user_prompt: str,
      output_schema: type[BaseModel],
      error_code: ErrorCode | None,
      error_prefix: str,
      operation_name: str,
  ) -> BaseModel:
    last_error: Exception | None = None
    for attempt in range(self.max_retries):
      try:
        start_time = time.monotonic()
        log.info(
            "[LLM_HTTP] structured_start | operation=%s attempt=%d prompt_chars=%d system_chars=%d",
            operation_name, attempt + 1, len(user_prompt), len(system_prompt),
        )
        response = await self._call_llm(
            chat_model, system_prompt, user_prompt, output_schema
        )
        elapsed_ms = (time.monotonic() - start_time) * 1000
        log.info(
            "%s completed: attempt=%d elapsed_ms=%.1f",
            operation_name,
            attempt + 1,
            elapsed_ms,
        )
        return response
      except Exception as e:
        last_error = e
        log.warning(
            "[LLM_HTTP] structured_error | operation=%s attempt=%d phase=%s exception_type=%s exception=%r elapsed_ms=%.1f",
            operation_name, attempt + 1, _timeout_phase(e), type(e).__name__, e,
            (time.monotonic() - start_time) * 1000 if 'start_time' in locals() else 0,
            exc_info=True,
        )
        if attempt < self.max_retries - 1:
          user_prompt = self._build_retry_prompt(user_prompt, str(e))

    prefix_msg = error_prefix if error_prefix else "操作失败"
    msg = f"{prefix_msg}{_exception_text(last_error) if last_error else 'unknown error'}"
    if error_code:
      raise BusinessException(error_code, msg) from last_error
    raise BusinessException(ErrorCode.AI_SERVICE_ERROR, msg) from last_error

  async def _call_llm(
      self,
      chat_model,
      system_prompt: str,
      user_prompt: str,
      output_schema: type[BaseModel],
  ) -> BaseModel:
    from langchain_core.output_parsers import PydanticOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    parser = PydanticOutputParser(pydantic_object=output_schema)
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "{input}\n\n{format_instructions}"),
    ])
    chain = prompt | chat_model | parser
    response = await chain.ainvoke({
        "input": user_prompt,
        "format_instructions": parser.get_format_instructions(),
    })
    return response

  def _build_retry_prompt(self, original_prompt: str, error_message: str) -> str:
    if self.retry_include_error:
      max_len = settings.ai.structured_error_message_max_length
      truncated_error = error_message[:max_len]
      return (
          f"{original_prompt}\n\n"
          f"[重试说明] 上一次回答无法被正确解析。错误信息：{truncated_error}。"
          f"请严格按照指定的JSON格式返回。"
      )
    return original_prompt
