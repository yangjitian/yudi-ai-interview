import json
import logging
import unicodedata
from pathlib import Path
from typing import Awaitable, Callable

from app.config.database import _async_session_factory
from app.core.errors import BusinessException, ErrorCode
from app.infrastructure.ai.embedding_client import EmbeddingClient
from app.infrastructure.ai.prompt_security import (
    DATA_BOUNDARY_INSTRUCTION,
)
from app.infrastructure.ai.provider_registry import get_plain_chat_client
from app.infrastructure.ai.structured_output import StructuredOutputInvoker
from app.models.kb_dto import (
    GeneratedKnowledgeBaseQuestion,
    GeneratedKnowledgeBaseQuestionList,
    KnowledgeBaseQuestionFollowUpDTO,
    QuestionGenerationConfig,
)
from app.models.knowledge_base import KnowledgeBaseEntity, KnowledgeBaseQuestionEntity
from app.repositories.kb_repository import KbRepository, KnowledgeBaseQuestionRepository
from app.services.kb.question_generation_state import QuestionGenerationStateService
from app.utils.prompt_sanitizer import PromptSanitizer


log = logging.getLogger(__name__)

RETRIEVAL_TOP_K = 12
RETRIEVAL_QUERY_TOP_K = 4
MAX_CONTEXT_CHARS = 5000
GENERATION_QUERIES = (
    "核心概念 定义 背景 原理",
    "关键流程 步骤 方法 工作机制",
    "规则约束 条件 边界 例外 限制",
    "典型案例 常见问题 应用场景 最佳实践",
)
_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "resources" / "prompts"


class KnowledgeBaseQuestionGenerationService:
  def __init__(
      self,
      session_factory=_async_session_factory,
      embedding_client: EmbeddingClient | None = None,
      structured_output_invoker: StructuredOutputInvoker | None = None,
      chat_client_factory: Callable[[str | None], Awaitable] = get_plain_chat_client,
      prompt_sanitizer: PromptSanitizer | None = None,
  ):
    self.session_factory = session_factory
    self.embedding_client = embedding_client or EmbeddingClient()
    self.structured_output_invoker = (
        structured_output_invoker or StructuredOutputInvoker()
    )
    self.chat_client_factory = chat_client_factory
    self.prompt_sanitizer = prompt_sanitizer or PromptSanitizer()
    self.system_prompt_template = self._load_prompt(
        "knowledgebase-question-generation-system.st"
    )
    self.user_prompt_template = self._load_prompt(
        "knowledgebase-question-generation-user.st"
    )

  async def execute_generation(
      self,
      kb_id: int,
      task_id: str,
      config: QuestionGenerationConfig,
  ) -> bool:
    knowledge_base = await self._find_knowledge_base(kb_id)
    if knowledge_base is None:
      raise BusinessException(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
    if knowledge_base.question_gen_task_id != task_id:
      log.info(
          "任务ID不匹配，放弃生成: kbId=%d msgTaskId=%s currentTaskId=%s",
          kb_id,
          task_id,
          knowledge_base.question_gen_task_id,
      )
      return False

    difficulty = self._normalize_difficulty(config.difficulty)
    follow_up_count = max(0, min(config.followUpCount, 5))
    category_limit = max(1, min(config.categoryLimit, 5))
    question_count = max(1, config.questionCount)

    context = await self._build_generation_context(kb_id)
    categories, existing_questions = await self._load_existing_questions(
        kb_id,
        difficulty,
    )
    async with self.session_factory() as session:
      state_service = QuestionGenerationStateService(
          KbRepository(session),
          KnowledgeBaseQuestionRepository(session),
      )
      if not await state_service.prepare(kb_id, task_id):
        return False

    chat_client = await self.chat_client_factory(config.llmProvider)
    seen_keys = {
        self._normalize_question_key(question)
        for question in existing_questions
    }
    saved_count = 0
    skipped_count = 0

    for _ in range(question_count):
      system_prompt, user_prompt = self._build_prompts(
          knowledge_base,
          difficulty,
          1,
          follow_up_count,
          category_limit,
          categories,
          existing_questions,
          context,
      )
      generated = await self.structured_output_invoker.invoke(
          chat_client,
          system_prompt,
          user_prompt,
          GeneratedKnowledgeBaseQuestionList,
          ErrorCode.INTERVIEW_QUESTION_GENERATION_FAILED,
          "知识库题库生成失败：",
          "知识库题库生成",
      )
      if not isinstance(generated, GeneratedKnowledgeBaseQuestionList):
        generated = GeneratedKnowledgeBaseQuestionList.model_validate(generated)
      questions, invalid_count = self._build_entities(
          knowledge_base,
          difficulty,
          context,
          follow_up_count,
          generated.questions or [],
      )
      skipped_count += invalid_count
      unique_questions = []
      for question in questions:
        key = self._normalize_question_key(question.question)
        if key in seen_keys:
          skipped_count += 1
          continue
        unique_questions.append(question)
      if len(unique_questions) > 1:
        skipped_count += len(unique_questions) - 1
      question = unique_questions[0] if unique_questions else None

      async with self.session_factory() as session:
        state_service = QuestionGenerationStateService(
            KbRepository(session),
            KnowledgeBaseQuestionRepository(session),
        )
        if not await state_service.save_progress(
            kb_id,
            task_id,
            question,
            skipped_count,
        ):
          return False
      if question is not None:
        saved_count += 1
        existing_questions.append(question.question)
        seen_keys.add(self._normalize_question_key(question.question))

    if saved_count == 0:
      raise BusinessException(
          ErrorCode.INTERVIEW_QUESTION_GENERATION_FAILED,
          "知识库题库生成结果无有效题干",
      )
    message = (
        f"已生成 {saved_count} 道题，跳过 {skipped_count} 道重复题"
        if skipped_count > 0
        else f"已生成 {saved_count} 道题"
    )
    async with self.session_factory() as session:
      state_service = QuestionGenerationStateService(
          KbRepository(session),
          KnowledgeBaseQuestionRepository(session),
      )
      return await state_service.complete(
          kb_id,
          task_id,
          saved_count,
          skipped_count,
          message,
      )

  async def _find_knowledge_base(self, kb_id: int) -> KnowledgeBaseEntity | None:
    async with self.session_factory() as session:
      return await KbRepository(session).find_by_id(kb_id)

  async def _build_generation_context(self, kb_id: int) -> str:
    chunks: list[dict] = []
    seen_texts: set[str] = set()
    for query in GENERATION_QUERIES:
      query_vector = await self.embedding_client.embed_text(query)
      async with self.session_factory() as session:
        hits = await KbRepository(session).search_chunks_by_vector(
            query_vector,
            [kb_id],
            RETRIEVAL_QUERY_TOP_K,
            0,
        )
      for hit in hits:
        text = hit.get("content")
        normalized = text.strip() if isinstance(text, str) else ""
        if not normalized or normalized in seen_texts:
          continue
        seen_texts.add(normalized)
        chunks.append(hit)
        if len(chunks) >= RETRIEVAL_TOP_K:
          break
      if len(chunks) >= RETRIEVAL_TOP_K:
        break

    if not chunks:
      raise BusinessException(
          ErrorCode.KNOWLEDGE_BASE_QUERY_FAILED,
          "知识库未检索到可用于生成题目的内容",
      )
    context = "\n\n---\n\n".join(chunk["content"] for chunk in chunks)
    if len(context) > MAX_CONTEXT_CHARS:
      return context[:MAX_CONTEXT_CHARS] + "\n...(知识库片段过长，已截断)"
    return context

  async def _load_existing_questions(
      self,
      kb_id: int,
      difficulty: str,
  ) -> tuple[list[tuple[str, int]], list[str]]:
    async with self.session_factory() as session:
      repository = KnowledgeBaseQuestionRepository(session)
      categories = await repository.find_category_counts(kb_id)
      questions = await repository.find_recent_by_difficulty(kb_id, difficulty)
      return categories[:10], [
          question.question.strip()
          for question in questions
          if question.question and question.question.strip()
      ]

  def _build_prompts(
      self,
      knowledge_base: KnowledgeBaseEntity,
      difficulty: str,
      question_count: int,
      follow_up_count: int,
      category_limit: int,
      categories: list[tuple[str, int]],
      existing_questions: list[str],
      context: str,
  ) -> tuple[str, str]:
    category_section = (
        "\n".join(f"- {category}（{count} 题）" for category, count in categories)
        if categories
        else "暂无已有方向"
    )
    question_section = (
        "\n".join(f"- {question}" for question in existing_questions)
        if existing_questions
        else "暂无已有题目"
    )
    safe_context = self.prompt_sanitizer.sanitize(context)
    wrapped_context = self.prompt_sanitizer.wrap_with_delimiters(
        "knowledge-base",
        safe_context,
    )
    values = {
        "knowledgeBaseName": self.prompt_sanitizer.sanitize(knowledge_base.name),
        "difficulty": difficulty,
        "questionCount": str(question_count),
        "followUpCount": str(follow_up_count),
        "categoryLimit": str(category_limit),
        "existingCategories": self.prompt_sanitizer.sanitize(category_section),
        "existingQuestions": self.prompt_sanitizer.sanitize(question_section),
        "context": f"{DATA_BOUNDARY_INSTRUCTION}\n{wrapped_context}",
    }
    user_prompt = self.user_prompt_template
    for name, value in values.items():
      user_prompt = user_prompt.replace("{" + name + "}", value)
    return self.system_prompt_template, user_prompt

  def _build_entities(
      self,
      knowledge_base: KnowledgeBaseEntity,
      difficulty: str,
      source_context: str,
      follow_up_count: int,
      generated: list[GeneratedKnowledgeBaseQuestion | None],
  ) -> tuple[list[KnowledgeBaseQuestionEntity], int]:
    entities: list[KnowledgeBaseQuestionEntity] = []
    batch_keys: set[str] = set()
    skipped_count = 0
    for item in generated:
      if item is None or item.question is None or not item.question.strip():
        skipped_count += 1
        continue
      question = item.question.strip()
      key = self._normalize_question_key(question)
      if key in batch_keys:
        skipped_count += 1
        continue
      batch_keys.add(key)
      category = self._trim_to_none(item.category) or (
          knowledge_base.name.strip() if knowledge_base.name.strip() else "未分类"
      )
      entities.append(KnowledgeBaseQuestionEntity(
          knowledge_base_id=knowledge_base.id,
          skill_id="knowledge-base",
          difficulty=difficulty,
          type=self._trim_to_none(item.type),
          category=category,
          question=question,
          topic_summary=self._trim_to_none(item.topicSummary),
          reference_answer=self._trim_to_none(item.referenceAnswer),
          key_points_json=self._write_string_list(item.keyPoints),
          scoring_rubric=self._trim_to_none(item.scoringRubric),
          follow_ups_json=self._write_follow_ups(item.followUps, follow_up_count),
          source_context=source_context,
          kb_content_hash=knowledge_base.file_hash,
          status="DRAFT",
      ))
    return entities, skipped_count

  @staticmethod
  def _write_string_list(values: list[str | None] | None) -> str:
    sanitized = [value.strip() for value in values or [] if value and value.strip()]
    return json.dumps(sanitized, ensure_ascii=False)

  @classmethod
  def _write_follow_ups(
      cls,
      values: list[KnowledgeBaseQuestionFollowUpDTO | None] | None,
      follow_up_count: int,
  ) -> str:
    if follow_up_count <= 0:
      return "[]"
    sanitized = []
    for value in values or []:
      if value is None or value.question is None or not value.question.strip():
        continue
      sanitized.append({
          "question": value.question.strip(),
          "referenceAnswer": cls._trim_to_none(value.referenceAnswer),
          "keyPoints": [
              item.strip()
              for item in value.keyPoints or []
              if item and item.strip()
          ],
          "scoringRubric": cls._trim_to_none(value.scoringRubric),
      })
      if len(sanitized) >= follow_up_count:
        break
    return json.dumps(sanitized, ensure_ascii=False)

  @staticmethod
  def _normalize_question_key(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).lower()
    return "".join(character for character in normalized if character.isalnum())

  @staticmethod
  def _normalize_difficulty(value: str | None) -> str:
    return value.strip() if value and value.strip() else "mid"

  @staticmethod
  def _trim_to_none(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None

  @staticmethod
  def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")
