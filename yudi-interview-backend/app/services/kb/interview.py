import json
import logging
import random
from collections import Counter
from dataclasses import dataclass

from app.core.errors import BusinessException, ErrorCode
from app.models.interview_dto import InterviewQuestionDTO, InterviewSessionDTO
from app.models.kb_dto import (
    CreateKnowledgeBaseInterviewRequest,
    KnowledgeBaseInterviewCapacityResponse,
    KnowledgeBaseInterviewCategoryOption,
    KnowledgeBaseInterviewFollowUpOption,
    KnowledgeBaseQuestionFollowUpDTO,
)
from app.models.knowledge_base import KnowledgeBaseQuestionEntity
from app.repositories.kb_repository import KbRepository, KnowledgeBaseQuestionRepository
from app.services.interview.session_service import InterviewSessionService


log = logging.getLogger(__name__)
MAX_FOLLOW_UP_COUNT = 5


@dataclass(frozen=True)
class _QuestionSource:
  question: KnowledgeBaseQuestionEntity
  key_points: list[str]
  follow_ups: list[KnowledgeBaseQuestionFollowUpDTO]


class KnowledgeBaseInterviewService:
  def __init__(
      self,
      kb_repository: KbRepository,
      question_repository: KnowledgeBaseQuestionRepository,
      interview_session_service: InterviewSessionService,
  ):
    self.kb_repository = kb_repository
    self.question_repository = question_repository
    self.interview_session_service = interview_session_service

  async def create_session(
      self,
      request: CreateKnowledgeBaseInterviewRequest,
  ) -> InterviewSessionDTO:
    await self._require_knowledge_base(request.knowledgeBaseId)
    category = self._trim_to_none(request.category)
    difficulty = self._normalize_difficulty(request.difficulty)
    sources = await self._select_active_questions(
        request.knowledgeBaseId,
        difficulty,
        category,
    )
    candidates = [
        source
        for source in sources
        if len(source.follow_ups) >= request.followUpCount
    ]
    if len(candidates) < request.mainQuestionCount:
      direction = category or "全部方向"
      raise BusinessException(
          ErrorCode.INTERVIEW_QUESTION_INSUFFICIENT,
          f"需要 {request.mainQuestionCount} 道主问题，但只有 {len(candidates)} 道同时满足："
          f"方向={direction}、难度={difficulty}、每题至少 {request.followUpCount} 个追问",
      )

    selected = list(candidates)
    random.shuffle(selected)
    questions = self._build_questions(
        selected[:request.mainQuestionCount],
        request.followUpCount,
    )
    return await self.interview_session_service.create_session_from_questions(
        questions=questions,
        llm_provider=request.llmProvider,
        skill_id="knowledge-base",
        difficulty=difficulty,
        knowledge_base_id=request.knowledgeBaseId,
        interview_category=category,
    )

  async def get_capacity(
      self,
      knowledge_base_id: int,
      category: str | None,
      difficulty: str | None,
      main_question_count: int,
  ) -> KnowledgeBaseInterviewCapacityResponse:
    await self._require_knowledge_base(knowledge_base_id)
    normalized_category = self._trim_to_none(category)
    normalized_difficulty = self._normalize_difficulty(difficulty)
    all_sources = await self._select_active_questions(
        knowledge_base_id,
        normalized_difficulty,
        None,
    )
    scoped_sources = [
        source
        for source in all_sources
        if normalized_category is None
        or source.question.category == normalized_category
    ]
    category_counts = Counter(
        normalized
        for source in all_sources
        if (normalized := self._trim_to_none(source.question.category)) is not None
    )
    categories = [
        KnowledgeBaseInterviewCategoryOption(
            category=name,
            availableQuestionCount=count,
        )
        for name, count in sorted(
            category_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    follow_up_options = []
    for follow_up_count in range(MAX_FOLLOW_UP_COUNT + 1):
      available_count = sum(
          len(source.follow_ups) >= follow_up_count
          for source in scoped_sources
      )
      follow_up_options.append(KnowledgeBaseInterviewFollowUpOption(
          followUpCount=follow_up_count,
          availableQuestionCount=available_count,
          selectable=main_question_count > 0 and available_count >= main_question_count,
      ))
    return KnowledgeBaseInterviewCapacityResponse(
        knowledgeBaseId=knowledge_base_id,
        category=normalized_category,
        difficulty=normalized_difficulty,
        mainQuestionCount=main_question_count,
        categories=categories,
        followUpOptions=follow_up_options,
    )

  async def _select_active_questions(
      self,
      knowledge_base_id: int,
      difficulty: str,
      category: str | None,
  ) -> list[_QuestionSource]:
    questions = await self.question_repository.find_active_for_interview(
        knowledge_base_id,
        difficulty,
        category,
    )
    return [self._to_question_source(question) for question in questions]

  async def _require_knowledge_base(self, knowledge_base_id: int) -> None:
    if await self.kb_repository.find_by_id(knowledge_base_id) is None:
      raise BusinessException(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

  def _build_questions(
      self,
      selected: list[_QuestionSource],
      follow_up_count: int,
  ) -> list[InterviewQuestionDTO]:
    questions: list[InterviewQuestionDTO] = []
    for source in selected:
      entity = source.question
      main_index = len(questions)
      questions.append(InterviewQuestionDTO(
          question_index=main_index,
          question=entity.question,
          type=self._default_string(entity.type, "KNOWLEDGE_BASE"),
          category=self._default_string(entity.category, "知识库"),
          topic_summary=entity.topic_summary,
          reference_answer=entity.reference_answer,
          key_points=source.key_points,
          scoring_rubric=entity.scoring_rubric,
          source_context=entity.source_context,
      ))
      for follow_up in self._pick_follow_ups(source.follow_ups, follow_up_count):
        questions.append(InterviewQuestionDTO(
            question_index=len(questions),
            question=follow_up.question or "",
            type=self._default_string(entity.type, "KNOWLEDGE_BASE"),
            category=self._default_string(entity.category, "知识库追问"),
            topic_summary=entity.topic_summary,
            is_follow_up=True,
            parent_question_index=main_index,
            reference_answer=follow_up.referenceAnswer,
            key_points=[item for item in follow_up.keyPoints or [] if item is not None],
            scoring_rubric=follow_up.scoringRubric,
            source_context=entity.source_context,
        ))
    return questions

  @staticmethod
  def _pick_follow_ups(
      pool: list[KnowledgeBaseQuestionFollowUpDTO],
      count: int,
  ) -> list[KnowledgeBaseQuestionFollowUpDTO]:
    if count <= 0:
      return []
    if len(pool) < count:
      raise BusinessException(
          ErrorCode.INTERVIEW_QUESTION_INSUFFICIENT,
          f"追问池在组装面试时发生变化，无法严格抽取 {count} 个追问",
      )
    if len(pool) == count:
      return list(pool)
    return random.sample(pool, count)

  @classmethod
  def _to_question_source(
      cls,
      question: KnowledgeBaseQuestionEntity,
  ) -> _QuestionSource:
    return _QuestionSource(
        question=question,
        key_points=cls._read_string_list(question.key_points_json),
        follow_ups=cls._read_usable_follow_ups(question.follow_ups_json),
    )

  @staticmethod
  def _read_string_list(value: str | None) -> list[str]:
    if value is None or not value.strip():
      return []
    try:
      parsed = json.loads(value)
      return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
      log.warning("解析题目要点失败")
      return []

  @staticmethod
  def _read_usable_follow_ups(
      value: str | None,
  ) -> list[KnowledgeBaseQuestionFollowUpDTO]:
    if value is None or not value.strip():
      return []
    try:
      parsed = json.loads(value)
      if not isinstance(parsed, list):
        return []
      follow_ups = []
      for item in parsed:
        if not isinstance(item, dict):
          continue
        follow_up = KnowledgeBaseQuestionFollowUpDTO.model_validate(item)
        if follow_up.question is None or not follow_up.question.strip():
          continue
        follow_up.question = follow_up.question.strip()
        follow_ups.append(follow_up)
      return follow_ups
    except (json.JSONDecodeError, TypeError, ValueError):
      log.warning("解析追问失败")
      return []

  @staticmethod
  def _normalize_difficulty(value: str | None) -> str:
    return value.strip() if value is not None and value.strip() else "mid"

  @staticmethod
  def _trim_to_none(value: str | None) -> str | None:
    if value is None or not value.strip():
      return None
    return value.strip()

  @staticmethod
  def _default_string(value: str | None, fallback: str) -> str:
    return value if value is not None and value.strip() else fallback
