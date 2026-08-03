import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator
from uuid import uuid4

from app.core.errors import BusinessException, ErrorCode
from app.models.kb_dto import (
    QuestionGenerationConfig,
    QuestionGenerationStatus,
    QuestionGenerationStatusResponse,
)
from app.models.knowledge_base import KnowledgeBaseEntity, KnowledgeBaseQuestionEntity
from app.repositories.kb_repository import KbRepository, KnowledgeBaseQuestionRepository
from app.utils.timezone_utils import get_beijing_now_naive


class QuestionGenerationStateService:
  SAFE_FAILURE_MESSAGE = "题目生成失败，请稍后重试"
  CANCEL_MESSAGE_PREFIX = "用户已停止生成"

  def __init__(
      self,
      kb_repository: KbRepository,
      question_repository: KnowledgeBaseQuestionRepository,
  ):
    self.kb_repository = kb_repository
    self.question_repository = question_repository
    self.session = kb_repository.session
    if question_repository.session is not self.session:
      raise ValueError("状态服务的 Repository 必须共享同一个数据库会话")

  async def submit(
      self,
      kb_id: int,
      config: QuestionGenerationConfig,
  ) -> str:
    async with self._transaction():
      knowledge_base = await self.kb_repository.find_by_id_for_update(kb_id)
      if knowledge_base is None:
        raise BusinessException(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
      if knowledge_base.vector_status != "COMPLETED":
        raise BusinessException(ErrorCode.BAD_REQUEST, "知识库尚未完成向量化")
      if knowledge_base.question_gen_status in {
          QuestionGenerationStatus.QUEUED.value,
          QuestionGenerationStatus.PROCESSING.value,
      }:
        raise BusinessException(
            ErrorCode.BAD_REQUEST,
            "知识库问题正在生成中，请勿重复提交",
        )

      task_id = str(uuid4())
      knowledge_base.question_gen_task_id = task_id
      knowledge_base.question_gen_status = QuestionGenerationStatus.QUEUED.value
      knowledge_base.question_gen_config = config.model_dump_json()
      knowledge_base.question_gen_error = None
      knowledge_base.question_gen_message = None
      knowledge_base.question_gen_saved_count = 0
      knowledge_base.question_gen_skipped_count = 0
      knowledge_base.question_gen_updated_at = get_beijing_now_naive()
      await self.kb_repository.save_generation_state(knowledge_base)
      return task_id

  async def claim(self, kb_id: int, task_id: str) -> bool:
    async with self._transaction():
      knowledge_base = await self.kb_repository.find_by_id_for_update(kb_id)
      if not self._matches(
          knowledge_base,
          task_id,
          QuestionGenerationStatus.QUEUED,
      ):
        return False
      knowledge_base.question_gen_status = QuestionGenerationStatus.PROCESSING.value
      knowledge_base.question_gen_error = None
      knowledge_base.question_gen_updated_at = get_beijing_now_naive()
      await self.kb_repository.save_generation_state(knowledge_base)
      return True

  async def prepare(self, kb_id: int, task_id: str) -> bool:
    async with self._transaction():
      knowledge_base = await self.kb_repository.find_by_id_for_update(kb_id)
      return self._matches(
          knowledge_base,
          task_id,
          QuestionGenerationStatus.PROCESSING,
      )

  async def save_progress(
      self,
      kb_id: int,
      task_id: str,
      question: KnowledgeBaseQuestionEntity | None,
      skipped_count: int,
  ) -> bool:
    async with self._transaction():
      knowledge_base = await self.kb_repository.find_by_id_for_update(kb_id)
      if not self._matches(
          knowledge_base,
          task_id,
          QuestionGenerationStatus.PROCESSING,
      ):
        return False
      if question is not None:
        question.knowledge_base_id = kb_id
        await self.question_repository.save(question)
        knowledge_base.question_gen_saved_count = (
            (knowledge_base.question_gen_saved_count or 0) + 1
        )
      knowledge_base.question_gen_skipped_count = skipped_count
      knowledge_base.question_gen_message = (
          f"已生成 {knowledge_base.question_gen_saved_count} 道题"
      )
      knowledge_base.question_gen_updated_at = get_beijing_now_naive()
      await self.kb_repository.save_generation_state(knowledge_base)
      return True

  async def complete(
      self,
      kb_id: int,
      task_id: str,
      saved_count: int,
      skipped_count: int,
      message: str,
  ) -> bool:
    async with self._transaction():
      knowledge_base = await self.kb_repository.find_by_id_for_update(kb_id)
      if not self._matches(
          knowledge_base,
          task_id,
          QuestionGenerationStatus.PROCESSING,
      ):
        return False

      knowledge_base.question_gen_status = QuestionGenerationStatus.COMPLETED.value
      knowledge_base.question_gen_error = None
      knowledge_base.question_gen_message = message
      knowledge_base.question_gen_saved_count = saved_count
      knowledge_base.question_gen_skipped_count = skipped_count
      knowledge_base.question_gen_updated_at = get_beijing_now_naive()
      await self.kb_repository.save_generation_state(knowledge_base)
      return True

  async def retry(self, kb_id: int, task_id: str) -> bool:
    async with self._transaction():
      knowledge_base = await self.kb_repository.find_by_id_for_update(kb_id)
      if not self._matches(
          knowledge_base,
          task_id,
          QuestionGenerationStatus.PROCESSING,
      ):
        return False
      knowledge_base.question_gen_status = QuestionGenerationStatus.QUEUED.value
      knowledge_base.question_gen_updated_at = get_beijing_now_naive()
      await self.kb_repository.save_generation_state(knowledge_base)
      return True

  async def fail(
      self,
      kb_id: int,
      task_id: str,
      error_message: str | None = None,
  ) -> bool:
    async with self._transaction():
      knowledge_base = await self.kb_repository.find_by_id_for_update(kb_id)
      if (
          knowledge_base is None
          or knowledge_base.question_gen_task_id != task_id
          or knowledge_base.question_gen_status not in {
              QuestionGenerationStatus.QUEUED.value,
              QuestionGenerationStatus.PROCESSING.value,
          }
      ):
        return False
      knowledge_base.question_gen_status = QuestionGenerationStatus.FAILED.value
      knowledge_base.question_gen_error = self.SAFE_FAILURE_MESSAGE
      knowledge_base.question_gen_updated_at = get_beijing_now_naive()
      await self.kb_repository.save_generation_state(knowledge_base)
      return True

  async def cancel(
      self,
      kb_id: int,
      task_id: str | None = None,
  ) -> QuestionGenerationStatusResponse:
    async with self._transaction():
      knowledge_base = await self.kb_repository.find_by_id_for_update(kb_id)
      if knowledge_base is None:
        raise BusinessException(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
      if task_id is not None and knowledge_base.question_gen_task_id != task_id:
        return self._to_status_response(knowledge_base)
      if self._is_cancelled(knowledge_base):
        return self._to_status_response(knowledge_base)
      if knowledge_base.question_gen_status not in {
          QuestionGenerationStatus.QUEUED.value,
          QuestionGenerationStatus.PROCESSING.value,
      }:
        return self._to_status_response(knowledge_base)

      config = self._read_config(knowledge_base.question_gen_config)
      saved_count = knowledge_base.question_gen_saved_count or 0
      if config is not None and saved_count >= config.questionCount:
        knowledge_base.question_gen_status = QuestionGenerationStatus.COMPLETED.value
        knowledge_base.question_gen_message = f"已生成 {saved_count} 道题"
      else:
        knowledge_base.question_gen_status = QuestionGenerationStatus.FAILED.value
        knowledge_base.question_gen_message = (
            f"{self.CANCEL_MESSAGE_PREFIX}，已保留 {saved_count} 道题"
        )
      knowledge_base.question_gen_error = None
      knowledge_base.question_gen_updated_at = get_beijing_now_naive()
      await self.kb_repository.save_generation_state(knowledge_base)
      return self._to_status_response(knowledge_base)

  async def get_status(self, kb_id: int) -> QuestionGenerationStatusResponse:
    knowledge_base = await self.kb_repository.find_by_id(kb_id)
    if knowledge_base is None:
      raise BusinessException(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
    return self._to_status_response(knowledge_base)

  async def get_config(
      self,
      kb_id: int,
      task_id: str,
  ) -> QuestionGenerationConfig:
    knowledge_base = await self.kb_repository.find_by_id(kb_id)
    if knowledge_base is None:
      raise BusinessException(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
    if knowledge_base.question_gen_task_id != task_id:
      raise BusinessException(ErrorCode.BAD_REQUEST, "题目生成任务已失效")
    config = self._read_config(knowledge_base.question_gen_config)
    if config is None:
      raise BusinessException(ErrorCode.INTERNAL_ERROR, "题目生成配置不存在")
    return config

  async def touch_queued_for_recovery(
      self,
      kb_id: int,
      task_id: str,
      threshold: datetime,
  ) -> bool:
    async with self._transaction():
      knowledge_base = await self.kb_repository.find_by_id_for_update(kb_id)
      if not self._matches(
          knowledge_base,
          task_id,
          QuestionGenerationStatus.QUEUED,
      ) or not self._is_stale(knowledge_base.question_gen_updated_at, threshold):
        return False
      knowledge_base.question_gen_updated_at = get_beijing_now_naive()
      await self.kb_repository.save_generation_state(knowledge_base)
      return True

  async def reset_stale_processing(
      self,
      kb_id: int,
      task_id: str,
      threshold: datetime,
  ) -> bool:
    async with self._transaction():
      knowledge_base = await self.kb_repository.find_by_id_for_update(kb_id)
      if not self._matches(
          knowledge_base,
          task_id,
          QuestionGenerationStatus.PROCESSING,
      ) or not self._is_stale(knowledge_base.question_gen_updated_at, threshold):
        return False
      knowledge_base.question_gen_status = QuestionGenerationStatus.QUEUED.value
      knowledge_base.question_gen_updated_at = get_beijing_now_naive()
      await self.kb_repository.save_generation_state(knowledge_base)
      return True

  @asynccontextmanager
  async def _transaction(self) -> AsyncIterator[None]:
    if self.session.in_transaction():
      yield
      return
    async with self.session.begin():
      yield

  @staticmethod
  def _matches(
      knowledge_base: KnowledgeBaseEntity | None,
      task_id: str,
      expected_status: QuestionGenerationStatus,
  ) -> bool:
    return (
        knowledge_base is not None
        and knowledge_base.question_gen_task_id == task_id
        and knowledge_base.question_gen_status == expected_status.value
    )

  @staticmethod
  def _is_stale(updated_at: datetime | None, threshold: datetime) -> bool:
    return updated_at is None or updated_at < threshold

  def _to_status_response(
      self,
      knowledge_base: KnowledgeBaseEntity,
  ) -> QuestionGenerationStatusResponse:
    raw_status = knowledge_base.question_gen_status or QuestionGenerationStatus.NONE.value
    response_status = (
        QuestionGenerationStatus.CANCELLED
        if self._is_cancelled(knowledge_base)
        else QuestionGenerationStatus(raw_status)
    )
    return QuestionGenerationStatusResponse(
        knowledgeBaseId=knowledge_base.id,
        questionGenStatus=response_status,
        questionGenTaskId=knowledge_base.question_gen_task_id,
        questionGenConfig=self._read_config(knowledge_base.question_gen_config),
        savedCount=knowledge_base.question_gen_saved_count or 0,
        skippedCount=knowledge_base.question_gen_skipped_count or 0,
        message=knowledge_base.question_gen_message,
        error=knowledge_base.question_gen_error,
        updatedAt=knowledge_base.question_gen_updated_at,
    )

  @classmethod
  def _is_cancelled(cls, knowledge_base: KnowledgeBaseEntity) -> bool:
    return (
        knowledge_base.question_gen_status == QuestionGenerationStatus.FAILED.value
        and bool(knowledge_base.question_gen_message)
        and knowledge_base.question_gen_message.startswith(cls.CANCEL_MESSAGE_PREFIX)
    )

  @staticmethod
  def _read_config(value: str | None) -> QuestionGenerationConfig | None:
    if value is None or not value.strip():
      return None
    try:
      return QuestionGenerationConfig.model_validate(json.loads(value))
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
      raise BusinessException(
          ErrorCode.INTERNAL_ERROR,
          "解析题目生成配置失败",
      ) from exc
