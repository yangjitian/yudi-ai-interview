import json
import logging

from app.core.errors import BusinessException, ErrorCode
from app.models.kb_dto import (
    CreateKnowledgeBaseQuestionRequest,
    KnowledgeBaseQuestionCategoryCount,
    KnowledgeBaseQuestionDTO,
    KnowledgeBaseQuestionFollowUpDTO,
    KnowledgeBaseQuestionStatus,
    UpdateKnowledgeBaseQuestionRequest,
)
from app.models.knowledge_base import KnowledgeBaseEntity, KnowledgeBaseQuestionEntity
from app.repositories.kb_repository import KbRepository, KnowledgeBaseQuestionRepository


log = logging.getLogger(__name__)


class KnowledgeBaseQuestionService:
  def __init__(
      self,
      kb_repository: KbRepository,
      question_repository: KnowledgeBaseQuestionRepository,
  ):
    self.kb_repository = kb_repository
    self.question_repository = question_repository

  async def list_questions(
      self,
      knowledge_base_id: int,
      status: KnowledgeBaseQuestionStatus | None = None,
      category: str | None = None,
      difficulty: str | None = None,
      keyword: str | None = None,
  ) -> list[KnowledgeBaseQuestionDTO]:
    rows = await self.question_repository.find_by_knowledge_base_id(
        knowledge_base_id,
        status.value if status is not None else None,
    )
    category_filter = self._trim_to_none(category)
    difficulty_filter = self._trim_to_none(difficulty)
    keyword_filter = self._trim_to_none(keyword)
    return [
        self._to_dto(question, knowledge_base_name)
        for question, knowledge_base_name in rows
        if (category_filter is None or question.category == category_filter)
        and (difficulty_filter is None or question.difficulty == difficulty_filter)
        and (keyword_filter is None or self._contains_keyword(question, keyword_filter))
    ]

  async def list_categories(
      self,
      knowledge_base_id: int,
  ) -> list[KnowledgeBaseQuestionCategoryCount]:
    rows = await self.question_repository.find_category_counts(knowledge_base_id)
    return [
        KnowledgeBaseQuestionCategoryCount(category=category, count=count)
        for category, count in rows
    ]

  async def create_question(
      self,
      knowledge_base_id: int,
      request: CreateKnowledgeBaseQuestionRequest,
  ) -> KnowledgeBaseQuestionDTO:
    knowledge_base = await self._get_knowledge_base(knowledge_base_id)
    question = KnowledgeBaseQuestionEntity(
        knowledge_base_id=knowledge_base.id,
        difficulty=self._normalize_difficulty(request.difficulty),
        type=self._trim_to_none(request.type),
        category=request.category.strip(),
        question=request.question.strip(),
        topic_summary=self._trim_to_none(request.topicSummary),
        reference_answer=self._trim_to_none(request.referenceAnswer),
        key_points_json=self._write_string_list(request.keyPoints),
        scoring_rubric=self._trim_to_none(request.scoringRubric),
        follow_ups_json=self._write_follow_ups(request.followUps),
        source_context=self._trim_to_none(request.sourceContext),
        kb_content_hash=knowledge_base.file_hash,
        status=(request.status or KnowledgeBaseQuestionStatus.DRAFT).value,
        skill_id="knowledge-base",
    )
    saved = await self.question_repository.save(question)
    return self._to_dto(saved, knowledge_base.name)

  async def update_question(
      self,
      question_id: int,
      request: UpdateKnowledgeBaseQuestionRequest,
  ) -> KnowledgeBaseQuestionDTO:
    question = await self._get_question(question_id)
    if request.difficulty is not None:
      question.difficulty = self._normalize_difficulty(request.difficulty)
    if request.type is not None:
      question.type = self._trim_to_none(request.type)
    if request.category is not None:
      if not request.category.strip():
        raise BusinessException(ErrorCode.BAD_REQUEST, "面试方向不能为空")
      question.category = request.category.strip()
    if request.question is not None:
      if not request.question.strip():
        raise BusinessException(ErrorCode.BAD_REQUEST, "题干不能为空")
      question.question = request.question.strip()
    if request.topicSummary is not None:
      question.topic_summary = self._trim_to_none(request.topicSummary)
    if request.referenceAnswer is not None:
      question.reference_answer = self._trim_to_none(request.referenceAnswer)
    if request.keyPoints is not None:
      question.key_points_json = self._write_string_list(request.keyPoints)
    if request.scoringRubric is not None:
      question.scoring_rubric = self._trim_to_none(request.scoringRubric)
    if request.followUps is not None:
      question.follow_ups_json = self._write_follow_ups(request.followUps)
    if request.sourceContext is not None:
      question.source_context = self._trim_to_none(request.sourceContext)
    if request.status is not None:
      question.status = request.status.value
    saved = await self.question_repository.save(question)
    return await self._to_dto_with_knowledge_base(saved)

  async def update_status(
      self,
      question_id: int,
      status: KnowledgeBaseQuestionStatus,
  ) -> KnowledgeBaseQuestionDTO:
    question = await self._get_question(question_id)
    question.status = status.value
    saved = await self.question_repository.save(question)
    return await self._to_dto_with_knowledge_base(saved)

  async def delete_question(self, question_id: int) -> None:
    question = await self._get_question(question_id)
    await self.question_repository.delete(question)

  async def _get_knowledge_base(self, knowledge_base_id: int) -> KnowledgeBaseEntity:
    knowledge_base = await self.kb_repository.find_by_id(knowledge_base_id)
    if knowledge_base is None:
      raise BusinessException(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)
    return knowledge_base

  async def _get_question(self, question_id: int) -> KnowledgeBaseQuestionEntity:
    question = await self.question_repository.find_by_id(question_id)
    if question is None:
      raise BusinessException(ErrorCode.INTERVIEW_QUESTION_NOT_FOUND)
    return question

  async def _to_dto_with_knowledge_base(
      self,
      question: KnowledgeBaseQuestionEntity,
  ) -> KnowledgeBaseQuestionDTO:
    knowledge_base_name = None
    if question.knowledge_base_id is not None:
      knowledge_base = await self.kb_repository.find_by_id(question.knowledge_base_id)
      if knowledge_base is not None:
        knowledge_base_name = knowledge_base.name
    return self._to_dto(question, knowledge_base_name)

  def _to_dto(
      self,
      question: KnowledgeBaseQuestionEntity,
      knowledge_base_name: str | None,
  ) -> KnowledgeBaseQuestionDTO:
    return KnowledgeBaseQuestionDTO(
        id=question.id,
        knowledgeBaseId=question.knowledge_base_id,
        knowledgeBaseName=knowledge_base_name,
        skillId=question.skill_id,
        difficulty=question.difficulty,
        type=question.type,
        category=question.category,
        question=question.question,
        topicSummary=question.topic_summary,
        referenceAnswer=question.reference_answer,
        keyPoints=self._read_string_list(question.key_points_json),
        scoringRubric=question.scoring_rubric,
        followUps=self._read_follow_ups(question.follow_ups_json),
        sourceContext=question.source_context,
        status=KnowledgeBaseQuestionStatus(question.status),
        createdAt=question.created_at,
        updatedAt=question.updated_at,
    )

  @staticmethod
  def _contains_keyword(
      question: KnowledgeBaseQuestionEntity,
      keyword: str,
  ) -> bool:
    lowered_keyword = keyword.lower()
    return any(
        value is not None and lowered_keyword in value.lower()
        for value in (
            question.question,
            question.reference_answer,
            question.scoring_rubric,
            question.topic_summary,
            question.category,
        )
    )

  @staticmethod
  def _normalize_difficulty(value: str | None) -> str:
    return value.strip() if value is not None and value.strip() else "mid"

  @staticmethod
  def _trim_to_none(value: str | None) -> str | None:
    if value is None or not value.strip():
      return None
    return value.strip()

  @staticmethod
  def _write_string_list(values: list[str | None] | None) -> str:
    sanitized = [value.strip() for value in values or [] if value and value.strip()]
    return json.dumps(sanitized, ensure_ascii=False)

  @classmethod
  def _write_follow_ups(
      cls,
      values: list[KnowledgeBaseQuestionFollowUpDTO | None] | None,
  ) -> str:
    sanitized: list[dict] = []
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
    return json.dumps(sanitized, ensure_ascii=False)

  @staticmethod
  def _read_string_list(value: str | None) -> list[str]:
    if value is None or not value.strip():
      return []
    try:
      parsed = json.loads(value)
      return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
      log.warning("解析题目字符串列表失败")
      return []

  @staticmethod
  def _read_follow_ups(value: str | None) -> list[KnowledgeBaseQuestionFollowUpDTO]:
    if value is None or not value.strip():
      return []
    try:
      parsed = json.loads(value)
      if not isinstance(parsed, list):
        return []
      return [
          KnowledgeBaseQuestionFollowUpDTO.model_validate(item)
          for item in parsed
          if isinstance(item, dict)
      ]
    except (json.JSONDecodeError, TypeError, ValueError):
      log.warning("解析追问列表失败")
      return []
