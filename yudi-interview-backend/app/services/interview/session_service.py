import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from app.core.errors import BusinessException, ErrorCode
from app.models.common import AsyncTaskStatus
from app.models.interview import InterviewAnswerEntity, InterviewSessionEntity, SessionStatus
from app.models.interview_dto import (
    CategoryScore,
    InterviewDetailDTO,
    InterviewQuestionDTO,
    InterviewReportDTO,
    InterviewSessionDTO,
    QuestionEvaluation,
    SubmitAnswerResponse,
)
from app.repositories.interview_repository import InterviewAnswerRepository, InterviewRepository
from app.infrastructure.redis.session_cache import SessionCache
from app.utils.timezone_utils import get_beijing_now

log = logging.getLogger(__name__)


class InterviewSessionService:
  def __init__(
      self,
      session_repo: InterviewRepository,
      answer_repo: InterviewAnswerRepository,
      session_cache: SessionCache,
  ):
    self.session_repo = session_repo
    self.answer_repo = answer_repo
    self.session_cache = session_cache

  async def create_session(
      self, skill_id: str, difficulty: str, question_count: int,
      resume_text: str | None, resume_id: int | None,
      llm_provider: str | None, force_create: bool,
      custom_categories: list[dict], jd_text: str | None,
  ) -> InterviewSessionDTO:
    from app.services.interview.question_generator import generate_questions_parallel
    from app.models.resume import ResumeEntity

    total_started_at = time.perf_counter()
    log.info(
        "[PERF] create_session start | skill_id=%s difficulty=%s question_count=%s jd=%s resume=%s",
        skill_id,
        difficulty,
        question_count,
        "yes" if jd_text else "no",
        "yes" if resume_text or resume_id else "no",
    )

    lookup_started_at = time.perf_counter()
    if resume_id and not force_create:
      existing = await self.session_repo.find_unfinished_session(resume_id)
      if existing:
        dto = await self._to_dto(existing)
        log.info(
            "[PERF] create_session - unfinished session lookup: %.3fs",
            time.perf_counter() - lookup_started_at,
        )
        log.info(
            "[PERF] create_session - total: %.3fs",
            time.perf_counter() - total_started_at,
        )
        return dto
    log.info(
        "[PERF] create_session - unfinished session lookup: %.3fs",
        time.perf_counter() - lookup_started_at,
    )

    session_id = self._generate_session_id()

    resume_started_at = time.perf_counter()
    effective_resume_text = resume_text or ""
    if resume_id and not effective_resume_text:
      resume = await self.session_repo.session.get(ResumeEntity, resume_id)
      if resume is None:
        raise BusinessException(ErrorCode.RESUME_NOT_FOUND)
      effective_resume_text = resume.resume_text or ""
    log.info(
        "[PERF] create_session - resume load: %.3fs",
        time.perf_counter() - resume_started_at,
    )

    generation_started_at = time.perf_counter()
    try:
      questions, is_fallback, fallback_reason = await generate_questions_parallel(
          session_id=session_id,
          skill_id=skill_id,
          difficulty=difficulty,
          question_count=question_count,
          resume_text=effective_resume_text,
          llm_provider=llm_provider,
          custom_categories=custom_categories,
          jd_text=jd_text,
      )
    finally:
      log.info(
          "[PERF] create_session - question generation (LLM): %.3fs",
          time.perf_counter() - generation_started_at,
      )

    question_dtos = [
        InterviewQuestionDTO(question=q["question"], category=q["category"])
        for q in questions
    ]
    if len(question_dtos) != question_count:
      log.error(
          "题目生成结果不完整，终止会话创建: expected=%d actual=%d",
          question_count,
          len(question_dtos),
      )
      raise BusinessException(
          ErrorCode.INTERVIEW_QUESTION_GENERATION_FAILED,
          "题目生成失败，请稍后重试",
      )

    cache_started_at = time.perf_counter()
    try:
      await self.session_cache.save_session(
          session_id=session_id,
          resume_text=effective_resume_text,
          resume_id=resume_id,
          questions=question_dtos,
          current_index=0,
          status=SessionStatus.CREATED.value,
          is_fallback=is_fallback,
          fallback_reason=fallback_reason,
          generation_mode="fallback_template" if is_fallback else "llm",
      )
    finally:
      log.info(
          "[PERF] create_session - cache write: %.3fs",
          time.perf_counter() - cache_started_at,
      )

    entity = InterviewSessionEntity(
        session_id=session_id,
        skill_id=skill_id,
        difficulty=difficulty,
        resume_id=resume_id,
        total_questions=len(question_dtos),
        current_question_index=0,
        status=SessionStatus.CREATED.value,
        questions_json=json.dumps([q.model_dump() for q in question_dtos], ensure_ascii=False),
        question_generation_fallback_reason=fallback_reason if is_fallback else None,
        llm_provider=llm_provider,
    )
    db_started_at = time.perf_counter()
    try:
      await self.session_repo.save(entity)
    finally:
      log.info(
          "[PERF] create_session - db write: %.3fs",
          time.perf_counter() - db_started_at,
      )

    log.info(
        "[PERF] create_session - total: %.3fs",
        time.perf_counter() - total_started_at,
    )

    return InterviewSessionDTO(
        session_id=session_id,
        resume_text=effective_resume_text,
        total_questions=len(question_dtos),
        current_index=0,
        questions=question_dtos,
        status=SessionStatus.CREATED.value,
        is_fallback=is_fallback,
        fallback_reason=fallback_reason,
        generation_mode="fallback_template" if is_fallback else "llm",
    )

  async def get_session(self, session_id: str) -> InterviewSessionDTO:
    cached = await self.session_cache.get_session(session_id)
    if cached:
      return cached

    entity = await self.session_repo.find_by_session_id(session_id)
    if entity is None:
      raise BusinessException(ErrorCode.INTERVIEW_SESSION_NOT_FOUND)

    return await self._to_dto(entity)

  async def submit_answer(
      self, session_id: str, question_index: int, answer: str
  ) -> SubmitAnswerResponse:
    session_dto = await self.get_session(session_id)
    questions = session_dto.questions

    if question_index < 0 or question_index >= len(questions):
      raise BusinessException(ErrorCode.INTERVIEW_QUESTION_NOT_FOUND)

    questions[question_index].answer = answer
    new_index = question_index + 1
    has_next = new_index < len(questions)
    next_question = questions[new_index] if has_next else None

    new_status = SessionStatus.COMPLETED.value if not has_next else SessionStatus.IN_PROGRESS.value

    await self.session_cache.update_questions(session_id, questions)
    await self.session_cache.update_current_index(session_id, new_index)
    if new_status == SessionStatus.COMPLETED.value:
      await self.session_cache.update_status(session_id, SessionStatus.COMPLETED.value)

    entity = await self.session_repo.find_by_session_id(session_id)
    if entity:
      entity.current_question_index = new_index
      entity.status = new_status
      if not has_next:
        from datetime import datetime, timezone
        entity.completed_at = get_beijing_now()
        entity.evaluate_status = "PENDING"

    q = questions[question_index]
    answer_entity = await self.answer_repo.upsert(
        session_id=entity.id,
        question_index=question_index,
        question=q.question,
        category=q.category,
        user_answer=answer,
    )

    if not has_next:
      await self.session_repo.session.commit()
      from app.infrastructure.redis.evaluate_producer import send_evaluate_task
      await send_evaluate_task(session_id)
      log.info("面试完成，评估任务已入队: %s", session_id)

    return SubmitAnswerResponse(
        has_next_question=has_next,
        next_question=next_question,
        new_index=new_index,
        total_questions=len(questions),
    )

  async def save_answer_draft(
      self, session_id: str, question_index: int, answer: str
  ) -> None:
    session_dto = await self.get_session(session_id)
    questions = session_dto.questions

    if question_index < 0 or question_index >= len(questions):
      raise BusinessException(ErrorCode.INTERVIEW_QUESTION_NOT_FOUND)

    questions[question_index].answer = answer
    await self.session_cache.update_questions(session_id, questions)

    entity = await self.session_repo.find_by_session_id(session_id)
    if entity:
      question = questions[question_index]
      await self.answer_repo.upsert(
          session_id=entity.id,
          question_index=question_index,
          question=question.question,
          category=question.category,
          user_answer=answer,
      )
      entity.status = SessionStatus.IN_PROGRESS.value

  async def complete_interview(self, session_id: str) -> None:
    session_dto = await self.get_session(session_id)
    if session_dto.status in (SessionStatus.COMPLETED.value, SessionStatus.EVALUATED.value):
      raise BusinessException(ErrorCode.INTERVIEW_ALREADY_COMPLETED)

    await self.session_cache.update_status(session_id, SessionStatus.COMPLETED.value)
    entity = await self.session_repo.find_by_session_id(session_id)
    if entity:
      entity.status = SessionStatus.COMPLETED.value
      entity.completed_at = get_beijing_now()
      entity.evaluate_status = "PENDING"

    await self.session_repo.session.commit()

    from app.infrastructure.redis.evaluate_producer import send_evaluate_task
    await send_evaluate_task(session_id)

  async def generate_report(self, session_id: str) -> InterviewReportDTO:
    from app.services.interview.unified_evaluation import UnifiedEvaluationService

    session_dto = await self.get_session(session_id)
    if session_dto.status not in (SessionStatus.COMPLETED.value, SessionStatus.EVALUATED.value):
      raise BusinessException(ErrorCode.INTERVIEW_NOT_COMPLETED)

    entity = await self.session_repo.find_by_session_id(session_id)
    eval_svc = UnifiedEvaluationService()
    report = await eval_svc.evaluate(
        session_id=session_id,
        resume_text=session_dto.resume_text,
        questions=session_dto.questions,
        skill_id=entity.skill_id if entity else None,
        llm_provider=entity.llm_provider if entity else None,
    )

    await self.session_cache.update_status(session_id, SessionStatus.EVALUATED.value)

    if entity:
      entity.status = SessionStatus.EVALUATED.value
      entity.evaluate_status = AsyncTaskStatus.COMPLETED.value
      entity.evaluate_error = None
      entity.overall_score = report.overall_score
      entity.overall_feedback = report.overall_feedback
      entity.strengths_json = json.dumps(report.strengths, ensure_ascii=False)
      entity.improvements_json = json.dumps(report.improvements, ensure_ascii=False)
      entity.reference_answers_json = json.dumps(report.reference_answers, ensure_ascii=False)
      await self.session_repo.session.flush()

      answer_repo = InterviewAnswerRepository(self.session_repo.session)
      for qe in report.question_evaluations:
        question = session_dto.questions[qe.question_index]
        await answer_repo.upsert(
            session_id=entity.id,
            question_index=qe.question_index,
            question=question.question,
            category=question.category,
            user_answer=question.answer or "",
        )
        await answer_repo.update_evaluation(
            session_id=entity.id,
            question_index=qe.question_index,
            score=qe.score,
            feedback=qe.feedback,
            reference_answer=qe.reference_answer,
            key_points_json=json.dumps(qe.key_points or [], ensure_ascii=False),
            eval_status=qe.eval_status or "COMPLETED",
        )

    return report

  async def get_detail(self, session_id: str) -> InterviewDetailDTO:
    session_dto = await self.get_session(session_id)
    entity = await self.session_repo.find_by_session_id(session_id)

    answers = []
    if entity:
      ans_entities = await self.answer_repo.find_by_session(entity.id)
      answers = [
          {
              "question_index": a.question_index,
              "question": a.question,
              "category": a.category,
              "user_answer": a.user_answer,
              "score": a.score,
              "feedback": a.feedback,
              "reference_answer": a.reference_answer,
              "key_points": self._load_json_list(a.key_points_json),
              "answered_at": a.answered_at,
          }
          for a in ans_entities
      ]

    report = None
    if entity and entity.overall_score is not None:
      category_scores = []
      question_evaluations = []
      strengths = []
      improvements = []
      reference_answers = []

      if entity.strengths_json:
        try:
          strengths = json.loads(entity.strengths_json)
        except Exception:
          pass
      if entity.improvements_json:
        try:
          improvements = json.loads(entity.improvements_json)
        except Exception:
          pass
      if entity.reference_answers_json:
        try:
          raw_reference_answers = json.loads(entity.reference_answers_json)
          for index, item in enumerate(raw_reference_answers):
            if isinstance(item, dict):
              reference_answers.append(item)
            elif isinstance(item, str):
              question = session_dto.questions[index] if index < len(session_dto.questions) else None
              reference_answers.append({
                  "question_index": index,
                  "question": question.question if question else "",
                  "reference_answer": item,
                  "key_points": [],
              })
        except Exception:
          pass

      category_values: dict[str, list[int]] = {}
      for answer in answers:
        if answer["question_index"] is None:
          continue
        category = answer["category"] or "其他"
        score = answer["score"] if answer["score"] is not None else 0
        category_values.setdefault(category, []).append(score)
        question_evaluations.append(QuestionEvaluation(
            question_index=answer["question_index"],
            question=answer["question"] or "",
            category=category,
            score=score,
            feedback=answer["feedback"] or "",
            reference_answer=answer["reference_answer"],
            key_points=answer["key_points"],
        ))
      category_scores = [
          CategoryScore(
              category=category,
              score=sum(scores) // len(scores),
              question_count=len(scores),
          )
          for category, scores in category_values.items()
      ]

      report = InterviewReportDTO(
          session_id=session_id,
          overall_score=entity.overall_score or 0,
          category_scores=category_scores,
          question_evaluations=question_evaluations,
          overall_feedback=entity.overall_feedback or "",
          strengths=strengths,
          improvements=improvements,
          reference_answers=reference_answers,
      )

    return InterviewDetailDTO(
        session_id=session_id,
        skill_id=entity.skill_id if entity else "",
        difficulty=entity.difficulty if entity else "mid",
        total_questions=session_dto.total_questions,
        current_index=session_dto.current_index,
        overall_score=entity.overall_score if entity else None,
        overall_feedback=entity.overall_feedback if entity else None,
        status=entity.status if entity else session_dto.status,
        evaluate_status=entity.evaluate_status if entity else None,
        evaluate_error=entity.evaluate_error if entity else None,
        questions=session_dto.questions,
        answers=answers,
        report=report,
        created_at=entity.created_at if entity else None,
        completed_at=entity.completed_at if entity else None,
    )

  async def _to_dto(self, entity: InterviewSessionEntity) -> InterviewSessionDTO:
    questions = []
    if entity.questions_json:
      try:
        questions = [
            InterviewQuestionDTO(**q)
            for q in json.loads(entity.questions_json)
        ]
      except Exception:
        pass

    answers = await self.answer_repo.find_by_session(entity.id)
    for answer in answers:
      index = answer.question_index
      if index is not None and 0 <= index < len(questions):
        questions[index].answer = answer.user_answer

    resume_text = ""
    if entity.resume_id:
      from app.models.resume import ResumeEntity
      resume = await self.session_repo.session.get(ResumeEntity, entity.resume_id)
      if resume:
        resume_text = resume.resume_text or ""

    return InterviewSessionDTO(
        session_id=entity.session_id,
        resume_text=resume_text,
        total_questions=entity.total_questions or 0,
        current_index=entity.current_question_index,
      questions=questions,
      status=entity.status,
      is_fallback=False,
      generation_mode="llm",
    )

  def _generate_session_id(self) -> str:
    import uuid
    return uuid.uuid4().hex[:16]

  @staticmethod
  def _load_json_list(value: str | None) -> list:
    if not value:
      return []
    try:
      result = json.loads(value)
      return result if isinstance(result, list) else []
    except Exception:
      return []
