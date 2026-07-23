import asyncio
import logging
import math
import os
import re
import time

from pydantic import BaseModel, Field

from app.core.errors import ErrorCode
from app.config.settings import get_settings
from app.infrastructure.ai.provider_registry import get_plain_chat_client
from app.infrastructure.ai.structured_output import StructuredOutputInvoker
from app.models.interview_dto import (
    CategoryScore,
    InterviewQuestionDTO,
    InterviewReportDTO,
    QuestionEvaluation,
)
from app.services.interview.prompt_engine import get_prompt_engine
from app.services.interview.skill_manager import get_skill_manager


log = logging.getLogger(__name__)


class EvaluationItem(BaseModel):
  question_index: int
  score: int = Field(ge=0, le=100)
  feedback: str
  reference_answer: str = ""
  key_points: list[str] = Field(default_factory=list)
  eval_status: str = "COMPLETED"


class EvaluationBatch(BaseModel):
  overall_feedback: str = ""
  strengths: list[str] = Field(default_factory=list)
  improvements: list[str] = Field(default_factory=list)
  question_evaluations: list[EvaluationItem] = Field(default_factory=list)


class EvaluationSummary(BaseModel):
  overall_feedback: str = ""
  strengths: list[str] = Field(default_factory=list)
  improvements: list[str] = Field(default_factory=list)


class UnifiedEvaluationService:
  EVAL_BATCH_TIMEOUT: int = int(os.getenv("EVAL_BATCH_TIMEOUT", "200"))
  EVAL_BATCH_SIZE: int = 8
  EVAL_SINGLE_TIMEOUT: int = int(os.getenv("EVAL_SINGLE_TIMEOUT", "95"))
  SUMMARY_TIMEOUT: int = 95

  def __init__(self) -> None:
    interview_settings = get_settings().interview
    self.EVAL_BATCH_SIZE = interview_settings.app_interview_evaluation_batch_size
    self.EVAL_STRATEGY = interview_settings.evaluation_strategy.lower()
    if self.EVAL_STRATEGY not in {"single", "batch"}:
      log.warning("[EVAL_CONFIG] 未知评估策略 %s，回退为 single", self.EVAL_STRATEGY)
      self.EVAL_STRATEGY = "single"
    self.EVAL_QUESTION_TIMEOUT = max(1, interview_settings.evaluation_question_timeout_seconds)
    self.EVAL_QUESTION_RETRY_COUNT = max(1, interview_settings.evaluation_question_retry_count)
    self.EVAL_QUESTION_CONCURRENCY = max(1, interview_settings.evaluation_question_concurrency)
    self.EVAL_QUESTION_REFERENCE_MAX_CHARS = max(200, interview_settings.evaluation_question_reference_max_chars)
    configured_batch_timeout = int(os.getenv("EVAL_BATCH_TIMEOUT", "200"))
    self.EVAL_SINGLE_TIMEOUT = int(os.getenv("EVAL_SINGLE_TIMEOUT", "95"))
    self.SUMMARY_TIMEOUT = interview_settings.evaluation_summary_timeout_seconds
    self.EVAL_RETRY_COUNT = max(1, interview_settings.evaluation_batch_retry_count)
    self.EVAL_RETRY_BACKOFF_SECONDS = float(os.getenv("EVAL_RETRY_BACKOFF_SECONDS", "2"))
    self.EVAL_BATCH_TIMEOUT_BUFFER_SECONDS = float(
        os.getenv("EVAL_BATCH_TIMEOUT_BUFFER_SECONDS", "5")
    )
    required_batch_timeout = (
        self.EVAL_SINGLE_TIMEOUT * self.EVAL_RETRY_COUNT
        + self.EVAL_RETRY_BACKOFF_SECONDS * (self.EVAL_RETRY_COUNT - 1)
        + self.EVAL_BATCH_TIMEOUT_BUFFER_SECONDS
    )
    self.EVAL_BATCH_TIMEOUT = max(configured_batch_timeout, math.ceil(required_batch_timeout))
    self.EVAL_SPLIT_FALLBACK_ENABLED = (
        os.getenv("EVAL_SPLIT_FALLBACK_ENABLED", "true").lower() == "true"
    )
    self._prompt_engine = get_prompt_engine()
    self._skill_mgr = get_skill_manager()
    self._structured_invoker = StructuredOutputInvoker(max_retries=1)
    if configured_batch_timeout < required_batch_timeout:
      log.warning(
          "[EVAL_CONFIG] 批次总超时不足，已提升有效预算 | configured=%ss effective=%ss "
          "formula=single_timeout*attempts+backoff+buffer",
          configured_batch_timeout, self.EVAL_BATCH_TIMEOUT,
      )
    log.info(
        "[EVAL_CONFIG] effective | strategy=%s question_timeout=%ss question_attempts=%d "
        "question_concurrency=%d question_reference_max_chars=%d batch_size=%d single_timeout=%ss attempts=%d "
        "retry_backoff=%.1fs batch_timeout=%ss summary_timeout=%ss split_fallback=%s",
        self.EVAL_STRATEGY, self.EVAL_QUESTION_TIMEOUT, self.EVAL_QUESTION_RETRY_COUNT,
        self.EVAL_QUESTION_CONCURRENCY, self.EVAL_QUESTION_REFERENCE_MAX_CHARS,
        self.EVAL_BATCH_SIZE, self.EVAL_SINGLE_TIMEOUT, self.EVAL_RETRY_COUNT,
        self.EVAL_RETRY_BACKOFF_SECONDS, self.EVAL_BATCH_TIMEOUT,
        self.SUMMARY_TIMEOUT, self.EVAL_SPLIT_FALLBACK_ENABLED,
    )

  async def evaluate(
      self,
      session_id: str,
      resume_text: str | None,
      questions: list[InterviewQuestionDTO],
      skill_id: str | None = None,
      llm_provider: str | None = None,
      completed_evaluations: dict[int, QuestionEvaluation] | None = None,
  ) -> InterviewReportDTO:
    if not questions:
      return self._empty_report(session_id)

    t0 = time.perf_counter()
    log.info("[PERF] Eval evaluate_start | session_id=%s questions=%d pending=%d", session_id, len(questions), len(questions) - len(completed_evaluations or {}))
    log.info("[PERF] evaluate 开始 | session_id=%s questions=%d skill_id=%s",
             session_id, len(questions), skill_id)

    result_by_index: dict[int, EvaluationItem] = {}
    for index, evaluation in (completed_evaluations or {}).items():
      if 0 <= index < len(questions):
        result_by_index[index] = EvaluationItem(
            question_index=index,
            score=evaluation.score,
            feedback=evaluation.feedback,
            reference_answer=evaluation.reference_answer or "",
            key_points=evaluation.key_points,
            eval_status="COMPLETED",
        )

    pending_questions = [
        (index, question)
        for index, question in enumerate(questions)
        if index not in result_by_index
    ]

    chat = None
    summary_reference_context = "无"
    batch_summary = EvaluationSummary()

    if pending_questions:
      chat = await get_plain_chat_client(llm_provider)

      truncated_resume = (resume_text or "")[:2000]
      log.info(
          "[PERF] 增量评估初始化: 待评估 %d/%d 道题 | strategy=%s",
          len(pending_questions),
          len(questions),
          self.EVAL_STRATEGY,
      )
      if self.EVAL_STRATEGY == "single":
        batch_result_by_index, batch_summary = await self._evaluate_questions_individually(
            chat, pending_questions, "", skill_id
        )
      else:
        t_ref = time.perf_counter()
        reference_context = await self._build_merged_reference_context(
            [question for _, question in pending_questions]
        )
        if reference_context == "无":
          reference_context = self._build_reference_context(skill_id)
        log.info("[PERF] evaluate reference 加载: %.3fs chars=%d",
                 time.perf_counter() - t_ref, len(reference_context))
        batch_result_by_index, batch_summary = await self._evaluate_questions_in_batches(
            chat, pending_questions, truncated_resume, reference_context
        )
      pending_indices = {index for index, _ in pending_questions}
      for idx, item in batch_result_by_index.items():
        if idx in pending_indices and idx not in result_by_index:
          result_by_index[idx] = item
    else:
      log.info("[PERF] 所有题目均已成功评估，跳过LLM调用")

    question_evaluations: list[QuestionEvaluation] = []
    for index, question in enumerate(questions):
      item = result_by_index.get(index)
      if item is None:
        log.warning("[WARN] LLM 未返回 question_index=%d 的评估结果，按 0 分标记 FAILED", index)
        item = EvaluationItem(
            question_index=index,
            score=0,
            feedback="该题评估失败，按 0 分计入总分，建议稍后重新评估或人工复核。",
            eval_status="FAILED",
        )
      question_evaluations.append(
          QuestionEvaluation(
              question_index=index,
              question=question.question,
              category=question.category or "其他",
              score=item.score,
              feedback=item.feedback,
              reference_answer=item.reference_answer,
              key_points=item.key_points,
              eval_status=item.eval_status,
          )
      )

    category_values: dict[str, list[int]] = {}
    for qe in question_evaluations:
      category_values.setdefault(qe.category, []).append(qe.score)
    category_scores = [
        CategoryScore(
            category=cat,
            score=sum(scores) // len(scores),
            question_count=len(scores),
        )
        for cat, scores in category_values.items()
    ]

    overall_score = (
        sum(qe.score for qe in question_evaluations) // len(question_evaluations)
        if question_evaluations else 0
    )

    if chat is None:
      chat = await get_plain_chat_client(llm_provider)
    log.info("[PERF] Eval summary_start | session_id=%s evaluations=%d reference_chars=%d", session_id, len(question_evaluations), len(summary_reference_context))
    summary = await self._generate_summary(
        chat,
        question_evaluations,
        resume_text or "",
        summary_reference_context,
        batch_summary,
    )

    log.info("[PERF] evaluate 完成 | session_id=%s elapsed=%.3fs overall_score=%d",
             session_id, time.perf_counter() - t0, overall_score)

    return InterviewReportDTO(
        session_id=session_id,
        overall_score=overall_score,
        category_scores=category_scores,
        question_evaluations=question_evaluations,
        overall_feedback=summary.overall_feedback,
        strengths=summary.strengths,
        improvements=summary.improvements,
        reference_answers=[
            {
                "question_index": qe.question_index,
                "question": qe.question,
                "reference_answer": qe.reference_answer or "",
                "key_points": qe.key_points,
            }
            for qe in question_evaluations
        ],
    )

  async def _generate_summary(
      self,
      chat,
      question_evaluations: list[QuestionEvaluation],
      resume_text: str,
      reference_context: str,
      fallback: EvaluationSummary,
  ) -> EvaluationSummary:
    category_values: dict[str, list[int]] = {}
    for evaluation in question_evaluations:
      category_values.setdefault(evaluation.category, []).append(evaluation.score)
    category_summary = "\n".join(
        f"- {category}: {sum(scores) // len(scores)}/100（{len(scores)}题）"
        for category, scores in category_values.items()
    ) or "无"
    question_highlights = "\n\n".join(
        f"Q{evaluation.question_index + 1} [{evaluation.category}]（{evaluation.score}分）：\n"
        f"题目：{evaluation.question}\n"
        f"评价：{evaluation.feedback[:200] if evaluation.feedback else '无'}"
        for evaluation in question_evaluations
    ) or "无"
    user_prompt = self._prompt_engine.render("interview-evaluation-summary-user", {
        "resume_text": resume_text[:800] if resume_text else "未提供",
        "reference_context": reference_context[:2000] if reference_context else "无",
        "category_summary": category_summary,
        "question_highlights": question_highlights,
        "fallback_overall_feedback": fallback.overall_feedback or "无",
        "fallback_strengths": "\n".join(f"- {item}" for item in fallback.strengths) or "无",
        "fallback_improvements": "\n".join(f"- {item}" for item in fallback.improvements) or "无",
    })
    system_prompt = (
        "你是一位资深技术面试官，请根据候选人的面试表现生成结构化的总结报告。"
        "总体评价需要结合具体表现，优势和改进建议各返回3到5条，内容具体且简洁。"
    )

    t0 = time.perf_counter()
    log.info("[PERF] Eval summary_llm_request | prompt_chars=%d system_chars=%d timeout=%ss", len(user_prompt), len(system_prompt), self.SUMMARY_TIMEOUT)
    try:
      result = await asyncio.wait_for(
          self._structured_invoker.invoke(
              chat_model=chat,
              system_prompt=system_prompt,
              user_prompt=user_prompt,
              output_schema=EvaluationSummary,
              error_code=ErrorCode.INTERVIEW_EVALUATION_FAILED,
              error_prefix="汇总生成失败：",
              operation_name="面试汇总",
          ),
          timeout=self.SUMMARY_TIMEOUT,
      )
      log.info("[PERF] Eval summary_llm_response | elapsed=%.3fs", time.perf_counter() - t0)
      summary = EvaluationSummary(
          overall_feedback=result.overall_feedback or fallback.overall_feedback,
          strengths=result.strengths or fallback.strengths,
          improvements=result.improvements or fallback.improvements,
      )
      log.info(
          "[PERF] 汇总生成完成: %.3fs | strengths=%d improvements=%d",
          time.perf_counter() - t0,
          len(summary.strengths),
          len(summary.improvements),
      )
      return summary
    except asyncio.TimeoutError:
      log.warning("[PERF] 汇总生成超时(%.0fs)，使用批量评估汇总", self.SUMMARY_TIMEOUT)
      return fallback
    except Exception as e:
      log.error("汇总生成失败，使用批量评估汇总: %s", e)
      return fallback

  async def _evaluate_questions_in_batches(
      self,
      chat,
      questions: list[tuple[int, InterviewQuestionDTO]],
      resume_text: str,
      reference_context: str,
  ) -> tuple[dict[int, EvaluationItem], EvaluationSummary]:
    result_by_index: dict[int, EvaluationItem] = {}
    all_feedbacks: list[str] = []
    all_strengths: list[str] = []
    all_improvements: list[str] = []

    batches = [
        questions[i:i + self.EVAL_BATCH_SIZE]
        for i in range(0, len(questions), self.EVAL_BATCH_SIZE)
    ]

    log.info(
        "[PERF] 分批评估开始 | 总题数=%d 批次数=%d 每批上限=%d",
        len(questions), len(batches), self.EVAL_BATCH_SIZE,
    )

    for batch_idx, batch in enumerate(batches):
      batch_indices = [idx for idx, _ in batch]
      t_batch = time.perf_counter()
      log.info(
          "[PERF] 批次[%d/%d] 开始 | 题目索引=%s",
          batch_idx + 1, len(batches), batch_indices,
      )
      batch_items, reports = await self._evaluate_batch_with_fallback(
          chat, batch, resume_text, reference_context,
          batch_label=f"{batch_idx + 1}/{len(batches)}",
      )
      result_by_index.update(batch_items)
      for report in reports:
        if report.overall_feedback:
          all_feedbacks.append(report.overall_feedback)
        all_strengths.extend(report.strengths)
        all_improvements.extend(report.improvements)
      log.info(
          "[PERF] 批次[%d/%d] 收敛 | 耗时=%.2fs | 成功题数=%d/%d",
          batch_idx + 1, len(batches), time.perf_counter() - t_batch,
          sum(item.eval_status == "COMPLETED" for item in batch_items.values()),
          len(batch_indices),
      )

    fallback_summary = EvaluationSummary(
        overall_feedback=all_feedbacks[0] if all_feedbacks else "",
        strengths=list(dict.fromkeys(all_strengths))[:5],
        improvements=list(dict.fromkeys(all_improvements))[:5],
    )
    return result_by_index, fallback_summary

  async def _evaluate_questions_individually(
      self,
      chat,
      questions: list[tuple[int, InterviewQuestionDTO]],
      resume_text: str,
      skill_id: str | None,
  ) -> tuple[dict[int, EvaluationItem], EvaluationSummary]:
    semaphore = asyncio.Semaphore(self.EVAL_QUESTION_CONCURRENCY)
    started_at = time.perf_counter()

    async def evaluate_one(
        question_index: int, question: InterviewQuestionDTO
    ) -> tuple[EvaluationItem, EvaluationBatch | None]:
      reference_context = self._build_question_reference_context(question, skill_id)
      async with semaphore:
        item_started_at = time.perf_counter()
        log.info(
            "[PERF] Eval question_start | index=%d reference_chars=%d timeout=%ss",
            question_index, len(reference_context), self.EVAL_QUESTION_TIMEOUT,
        )
        try:
          result = await self._evaluate_question_with_retries(
              chat, question_index, question, resume_text, reference_context
          )
          items = [
              item for item in result.question_evaluations
              if item.question_index == question_index
          ]
          if not items:
            raise ValueError(f"结构化结果缺少题目索引: {question_index}")
          item = items[0]
          item.eval_status = "COMPLETED"
          log.info(
              "[PERF] Eval question_complete | index=%d elapsed=%.3fs prompt_reference_chars=%d",
              question_index, time.perf_counter() - item_started_at, len(reference_context),
          )
          return item, result
        except Exception as error:
          reason = "评估超时" if self._evaluation_error_kind(error) == "timeout" else "评估异常"
          log.error(
              "[PERF] Eval question_failed | index=%d elapsed=%.3fs reason=%s "
              "exception_type=%s exception=%s",
              question_index, time.perf_counter() - item_started_at, reason,
              type(error).__name__, str(error)[:200],
          )
          return EvaluationItem(
              question_index=question_index,
              score=0,
              feedback=f"该题{reason}，按 0 分计入总分，建议稍后重新评估或人工复核。",
              eval_status="FAILED",
          ), None

    results = await asyncio.gather(*(
        evaluate_one(question_index, question)
        for question_index, question in questions
    ))
    result_by_index = {item.question_index: item for item, _ in results}
    reports = [report for _, report in results if report is not None]
    log.info(
        "[PERF] Eval questions_parallel_complete | questions=%d concurrency=%d "
        "elapsed=%.3fs completed=%d failed=%d",
        len(questions), self.EVAL_QUESTION_CONCURRENCY, time.perf_counter() - started_at,
        sum(item.eval_status == "COMPLETED" for item in result_by_index.values()),
        sum(item.eval_status == "FAILED" for item in result_by_index.values()),
    )
    return result_by_index, EvaluationSummary(
        overall_feedback=next(
            (report.overall_feedback for report in reports if report.overall_feedback), ""
        ),
        strengths=list(dict.fromkeys(
            item for report in reports for item in report.strengths
        ))[:5],
        improvements=list(dict.fromkeys(
            item for report in reports for item in report.improvements
        ))[:5],
    )

  async def _evaluate_question_with_retries(
      self,
      chat,
      question_index: int,
      question: InterviewQuestionDTO,
      resume_text: str,
      reference_context: str,
  ) -> EvaluationBatch:
    repair_instruction = ""
    for attempt in range(1, self.EVAL_QUESTION_RETRY_COUNT + 1):
      started_at = time.perf_counter()
      try:
        log.info(
            "[PERF] Eval question_llm_request | index=%d attempt=%d/%d timeout=%ss",
            question_index, attempt, self.EVAL_QUESTION_RETRY_COUNT,
            self.EVAL_QUESTION_TIMEOUT,
        )
        return await asyncio.wait_for(
            self._evaluate_questions_batch(
                chat, [(question_index, question)], resume_text,
                reference_context, repair_instruction
            ),
            timeout=self.EVAL_QUESTION_TIMEOUT,
        )
      except Exception as error:
        error_kind = self._evaluation_error_kind(error)
        log.warning(
            "[EVAL_RETRY] question_attempt_failed | index=%d attempt=%d/%d kind=%s "
            "elapsed=%.3fs exception_type=%s exception=%s",
            question_index, attempt, self.EVAL_QUESTION_RETRY_COUNT, error_kind,
            time.perf_counter() - started_at, type(error).__name__, str(error)[:200],
        )
        if attempt == self.EVAL_QUESTION_RETRY_COUNT:
          raise
        if self.EVAL_RETRY_BACKOFF_SECONDS > 0:
          await asyncio.sleep(self.EVAL_RETRY_BACKOFF_SECONDS)
        repair_instruction = (
            "\n\n[重试说明] 请只返回符合指定JSON Schema的完整JSON，不要添加额外说明。"
            if error_kind == "structured_parse" else ""
        )
    raise RuntimeError("单题评估重试流程未返回结果")

  async def _evaluate_batch_with_fallback(
      self,
      chat,
      batch: list[tuple[int, InterviewQuestionDTO]],
      resume_text: str,
      reference_context: str,
      batch_label: str,
  ) -> tuple[dict[int, EvaluationItem], list[EvaluationBatch]]:
    batch_indices = [idx for idx, _ in batch]
    try:
      result = await asyncio.wait_for(
          self._evaluate_batch_with_retries(
              chat, batch, resume_text, reference_context, batch_label
          ),
          timeout=self.EVAL_BATCH_TIMEOUT,
      )
      expected_indices = set(batch_indices)
      items = {
          item.question_index: item
          for item in result.question_evaluations
          if item.question_index in expected_indices
      }
      missing_indices = expected_indices - set(items)
      if missing_indices:
        raise ValueError(f"结构化结果缺少题目索引: {sorted(missing_indices)}")
      for item in items.values():
        item.eval_status = "COMPLETED"
      return items, [result]
    except Exception as error:
      if self.EVAL_SPLIT_FALLBACK_ENABLED and len(batch) > 1:
        middle = len(batch) // 2
        left_batch = batch[:middle]
        right_batch = batch[middle:]
        log.warning(
            "[EVAL_FALLBACK] 批次失败，二分重试 | batch=%s indices=%s "
            "left=%s right=%s error_type=%s",
            batch_label, batch_indices,
            [idx for idx, _ in left_batch], [idx for idx, _ in right_batch],
            type(error).__name__,
        )
        left_items, left_reports = await self._evaluate_batch_with_fallback(
            chat, left_batch, resume_text, reference_context, f"{batch_label}.L"
        )
        right_items, right_reports = await self._evaluate_batch_with_fallback(
            chat, right_batch, resume_text, reference_context, f"{batch_label}.R"
        )
        left_items.update(right_items)
        return left_items, left_reports + right_reports

      reason = "评估超时" if self._evaluation_error_kind(error) == "timeout" else "评估异常"
      log.error(
          "[EVAL_FALLBACK] 单题评估失败，按 0 分计入总分 | batch=%s indices=%s "
          "reason=%s error_type=%s error=%s",
          batch_label, batch_indices, reason, type(error).__name__, str(error)[:200],
      )
      return {
          idx: EvaluationItem(
              question_index=idx,
              score=0,
              feedback=f"该题{reason}，按 0 分计入总分，建议稍后重新评估或人工复核。",
              eval_status="FAILED",
          )
          for idx, _ in batch
      }, []

  async def _evaluate_batch_with_retries(
      self,
      chat,
      batch: list[tuple[int, InterviewQuestionDTO]],
      resume_text: str,
      reference_context: str,
      batch_label: str,
  ) -> EvaluationBatch:
    repair_instruction = ""
    for attempt in range(1, self.EVAL_RETRY_COUNT + 1):
      attempt_started_at = time.perf_counter()
      try:
        log.info(
            "[PERF] Eval batch_llm_request | batch=%s attempt=%d/%d "
            "question_count=%d attempt_timeout=%ss",
            batch_label, attempt, self.EVAL_RETRY_COUNT,
            len(batch), self.EVAL_SINGLE_TIMEOUT,
        )
        result = await asyncio.wait_for(
            self._evaluate_questions_batch(
                chat, batch, resume_text, reference_context, repair_instruction
            ),
            timeout=self.EVAL_SINGLE_TIMEOUT,
        )
        log.info(
            "[PERF] Eval batch_llm_response | batch=%s attempt=%d/%d elapsed=%.3fs",
            batch_label, attempt, self.EVAL_RETRY_COUNT,
            time.perf_counter() - attempt_started_at,
        )
        return result
      except Exception as error:
        error_kind = self._evaluation_error_kind(error)
        log.warning(
            "[EVAL_RETRY] attempt_failed | batch=%s attempt=%d/%d kind=%s "
            "elapsed=%.3fs exception_type=%s exception=%s",
            batch_label, attempt, self.EVAL_RETRY_COUNT, error_kind,
            time.perf_counter() - attempt_started_at,
            type(error).__name__, str(error)[:200],
            exc_info=True,
        )
        if attempt == self.EVAL_RETRY_COUNT:
          raise
        if error_kind == "timeout" and self.EVAL_RETRY_BACKOFF_SECONDS > 0:
          await asyncio.sleep(self.EVAL_RETRY_BACKOFF_SECONDS)
        repair_instruction = (
            "\n\n[重试说明] 上一次响应未通过结构化解析。"
            "请只返回符合指定JSON Schema的完整JSON，不要添加Markdown代码块或额外说明。"
            if error_kind == "structured_parse" else ""
        )
    raise RuntimeError("评估重试流程未返回结果")

  @staticmethod
  def _evaluation_error_kind(error: BaseException) -> str:
    current: BaseException | None = error
    while current is not None:
      name = type(current).__name__
      if name in {"TimeoutError", "ReadTimeout", "ConnectTimeout", "APITimeoutError"}:
        return "timeout"
      if name in {"ValidationError", "OutputParserException", "JSONDecodeError"}:
        return "structured_parse"
      current = current.__cause__ or current.__context__
    return "call_error"

  async def _evaluate_questions_batch(
      self,
      chat,
      questions: list[tuple[int, InterviewQuestionDTO]],
      resume_text: str,
      reference_context: str,
      repair_instruction: str = "",
  ) -> EvaluationBatch:
    qa_records = self._build_batch_qa_records(questions)
    user_prompt = self._prompt_engine.render("interview-evaluation-user", {
        "resume_text": resume_text,
        "qa_records": qa_records,
        "total_questions": len(questions),
        "reference_context": reference_context or "无",
    })
    system_prompt = self._prompt_engine.render("interview-evaluation-system", {})
    user_prompt += repair_instruction

    log.info(
        "[PERF] 批量评估开始 | 题目数: %d | 参考资料: %d 字",
        len(questions),
        len(reference_context),
    )
    t0 = time.perf_counter()
    effective_timeout = (
        self.EVAL_QUESTION_TIMEOUT
        if self.EVAL_STRATEGY == "single" and len(questions) == 1
        else self.EVAL_SINGLE_TIMEOUT
    )
    log.info(
        "[PERF] Eval batch_prompt | question_count=%d prompt_chars=%d "
        "system_chars=%d attempt_timeout=%ss",
        len(questions), len(user_prompt), len(system_prompt), effective_timeout,
    )
    result = await self._structured_invoker.invoke(
        chat_model=chat,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        output_schema=EvaluationBatch,
        error_code=ErrorCode.INTERVIEW_EVALUATION_FAILED,
        error_prefix="批量评估失败：",
        operation_name="批量评估",
    )

    log.info(
        "[PERF] 批量评估完成 | 耗时: %.2fs | 返回题数: %d",
        time.perf_counter() - t0,
        len(result.question_evaluations),
    )
    return result

  @staticmethod
  def _build_batch_qa_records(
      questions: list[tuple[int, InterviewQuestionDTO]],
  ) -> str:
    parts = []
    for question_index, question in questions:
      answer_text = (question.answer or "").strip()
      if not answer_text:
        answer_text = "（未作答）"
      parts.append(
          f"[题目 {question_index}][类别: {question.category or '其他'}]\n"
          f"问题：{question.question}\n"
          f"回答：{answer_text}"
      )
    return "\n\n".join(parts)

  async def _build_merged_reference_context(
      self,
      questions: list[InterviewQuestionDTO],
  ) -> str:
    skill_ids = []
    for question in questions:
      question_skill_id = getattr(question, "skill_id", None)
      if question_skill_id and question_skill_id not in skill_ids:
        skill_ids.append(question_skill_id)

    if not skill_ids:
      return "无"

    sections = []
    for question_skill_id in skill_ids:
      content = self._build_reference_context(question_skill_id)
      if content and content != "无":
        sections.append(f"## 技能方向：{question_skill_id}\n{content}")
    return "\n\n".join(sections) if sections else "无"

  def _build_reference_context(self, skill_id: str | None) -> str:
    if not skill_id:
      return "无"
    references = self._skill_mgr.get_reference_content(skill_id)
    if not references:
      return "无"
    return "\n\n".join(references.values())[:6000]

  def _build_question_reference_context(
      self, question: InterviewQuestionDTO, skill_id: str | None
  ) -> str:
    if not skill_id:
      return "无"
    references = self._skill_mgr.get_reference_content(skill_id)
    if not references:
      return "无"

    query_terms = self._reference_terms(
        f"{question.category or ''} {question.question}"
    )
    candidates: list[tuple[int, int, str]] = []
    order = 0
    for ref_name, content in references.items():
      for section in re.split(r"\n\s*\n", content):
        section = section.strip()
        if not section:
          continue
        score = len(query_terms & self._reference_terms(f"{ref_name} {section}"))
        candidates.append((score, order, section))
        order += 1

    selected = sorted(candidates, key=lambda item: (-item[0], item[1]))
    if any(score > 0 for score, _, _ in selected):
      selected = [item for item in selected if item[0] > 0]
    parts: list[str] = []
    current_chars = 0
    for _, _, section in selected:
      separator_chars = 2 if parts else 0
      remaining = (
          self.EVAL_QUESTION_REFERENCE_MAX_CHARS - current_chars - separator_chars
      )
      if remaining <= 0:
        break
      parts.append(section[:remaining])
      current_chars += separator_chars + len(parts[-1])
    return "\n\n".join(parts) or "无"

  @staticmethod
  def _reference_terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(re.findall(r"[a-z0-9_+#.-]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    terms.update(chinese[index:index + 2] for index in range(len(chinese) - 1))
    return terms

  @staticmethod
  def _empty_report(session_id: str) -> InterviewReportDTO:
    return InterviewReportDTO(session_id=session_id, overall_score=0)


async def evaluate_interview(
    session_id: str,
    resume_text: str | None,
    questions: list[InterviewQuestionDTO],
) -> InterviewReportDTO:
  return await UnifiedEvaluationService().evaluate(session_id, resume_text, questions)
