import json
import logging

from app.core.errors import BusinessException, ErrorCode
from app.models.interview_dto import InterviewQuestionDTO
from app.models.voice_dto import (
    VoiceEvaluationAnswerDetail,
    VoiceEvaluationDetailDTO,
)
from app.models.voice_interview import (
    VoiceInterviewEvaluationEntity,
    VoiceInterviewMessageEntity,
)
from app.repositories.voice_evaluation_repository import VoiceEvaluationRepository
from app.repositories.voice_message_repository import VoiceMessageRepository
from app.repositories.voice_session_repository import VoiceSessionRepository
from app.services.interview.unified_evaluation import UnifiedEvaluationService
from app.utils.timezone_utils import get_beijing_now_naive

log = logging.getLogger(__name__)


class VoiceInterviewEvaluationService:
  def __init__(
      self,
      session_repo: VoiceSessionRepository,
      message_repo: VoiceMessageRepository,
      evaluation_repo: VoiceEvaluationRepository,
      unified_evaluation_service: UnifiedEvaluationService | None = None,
  ):
    self.session_repo = session_repo
    self.message_repo = message_repo
    self.evaluation_repo = evaluation_repo
    self.unified_evaluation_service = (
        unified_evaluation_service or UnifiedEvaluationService()
    )

  async def generate_evaluation(self, session_id: int) -> int | None:
    session = await self.session_repo.find_by_id(session_id)
    if session is None:
      raise BusinessException(ErrorCode.VOICE_SESSION_NOT_FOUND)

    messages = await self.message_repo.find_by_session_id(session_id)
    if not messages:
      await self._save_empty_evaluation(session_id, session)
      return None

    questions = self.build_questions(messages)
    # 结束读取事务后再调用 LLM，避免耗时外部调用占用数据库事务。
    await self.session_repo.commit()
    try:
      report = await self.unified_evaluation_service.evaluate(
          session_id=str(session_id),
          resume_text=None,
          questions=questions,
          skill_id=session.skill_id,
          llm_provider=session.llm_provider,
      )
      question_by_index = {
          question.question_index: question for question in questions
      }
      question_items = []
      for item in report.question_evaluations:
        question = question_by_index.get(item.question_index)
        question_items.append({
            "questionIndex": item.question_index,
            "question": item.question,
            "category": item.category,
            "userAnswer": question.answer if question is not None else None,
            "score": item.score,
            "feedback": item.feedback,
        })
      reference_items = [{
          "questionIndex": self._value(item, "questionIndex", "question_index"),
          "question": item.get("question", ""),
          "referenceAnswer": (
              self._value(item, "referenceAnswer", "reference_answer") or ""
          ),
          "keyPoints": self._value(item, "keyPoints", "key_points") or [],
      } for item in report.reference_answers]

      entity = VoiceInterviewEvaluationEntity(
          session_id=session_id,
          overall_score=report.overall_score,
          overall_feedback=report.overall_feedback,
          question_evaluations_json=json.dumps(question_items, ensure_ascii=False),
          strengths_json=json.dumps(report.strengths, ensure_ascii=False),
          improvements_json=json.dumps(report.improvements, ensure_ascii=False),
          reference_answers_json=json.dumps(reference_items, ensure_ascii=False),
          interviewer_role=session.role_type,
          interview_date=session.start_time,
          created_at=get_beijing_now_naive(),
      )
      await self.evaluation_repo.save(entity)
      await self.session_repo.commit()
      return report.overall_score
    except BusinessException:
      raise
    except Exception as exc:
      log.exception("生成语音面试评估失败: sessionId=%s", session_id)
      raise BusinessException(
          ErrorCode.VOICE_EVALUATION_FAILED,
          f"生成评估失败: {exc}",
      ) from exc

  async def _save_empty_evaluation(self, session_id: int, session) -> None:
    entity = await self.evaluation_repo.find_by_session_id(session_id)
    if entity is None:
      entity = VoiceInterviewEvaluationEntity(
          session_id=session_id,
          created_at=get_beijing_now_naive(),
      )
    entity.overall_score = None
    entity.overall_feedback = "本次语音面试未形成有效对话记录，暂无可评估内容。"
    entity.question_evaluations_json = "[]"
    entity.strengths_json = "[]"
    entity.improvements_json = json.dumps(
        ["请先完成至少一轮有效问答后再生成评估。"],
        ensure_ascii=False,
    )
    entity.reference_answers_json = "[]"
    entity.interviewer_role = session.role_type
    entity.interview_date = session.start_time
    await self.evaluation_repo.save(entity)
    await self.session_repo.commit()

  @staticmethod
  def build_questions(
      messages: list[VoiceInterviewMessageEntity],
  ) -> list[InterviewQuestionDTO]:
    questions: list[InterviewQuestionDTO] = []
    pending: tuple[str, str] | None = None

    for message in messages:
      ai_text = VoiceInterviewEvaluationService._trim_to_none(
          message.ai_generated_text
      )
      user_text = VoiceInterviewEvaluationService._trim_to_none(
          message.user_recognized_text
      )

      if pending is not None and user_text is not None:
        questions.append(InterviewQuestionDTO(
            question=pending[0],
            category=pending[1],
            question_index=len(questions),
            answer=user_text,
        ))
        pending = None
        if ai_text is not None:
          pending = (ai_text, VoiceInterviewEvaluationService.infer_category(ai_text))
        continue

      if pending is not None:
        questions.append(InterviewQuestionDTO(
            question=pending[0],
            category=pending[1],
            question_index=len(questions),
            answer=None,
        ))
        pending = None

      if ai_text is not None and user_text is not None:
        questions.append(InterviewQuestionDTO(
            question=ai_text,
            category=VoiceInterviewEvaluationService.infer_category(ai_text),
            question_index=len(questions),
            answer=user_text,
        ))
      elif ai_text is not None:
        pending = (ai_text, VoiceInterviewEvaluationService.infer_category(ai_text))
      elif user_text is not None:
        questions.append(InterviewQuestionDTO(
            question="",
            category="综合",
            question_index=len(questions),
            answer=user_text,
        ))

    if pending is not None:
      questions.append(InterviewQuestionDTO(
          question=pending[0],
          category=pending[1],
          question_index=len(questions),
          answer=None,
      ))
    return questions

  @staticmethod
  def infer_category(ai_text: str | None) -> str:
    if ai_text is None:
      return "综合"
    if any(keyword in ai_text for keyword in ("项目", "实习", "工作经历")):
      return "项目深挖"
    if "自我介绍" in ai_text or "介绍一下自己" in ai_text:
      return "自我介绍"
    if any(keyword in ai_text for keyword in ("职业规划", "为什么", "优缺点")):
      return "HR问题"
    return "技术问题"

  @staticmethod
  def build_detail(
      entity: VoiceInterviewEvaluationEntity,
  ) -> VoiceEvaluationDetailDTO:
    try:
      question_items = json.loads(entity.question_evaluations_json or "[]")
      strengths = json.loads(entity.strengths_json or "[]")
      improvements = json.loads(entity.improvements_json or "[]")
      reference_items = json.loads(entity.reference_answers_json or "[]")
      references = {
          VoiceInterviewEvaluationService._value(item, "questionIndex", "question_index"): item
          for item in reference_items
      }
      answers = []
      for item in question_items:
        question_index = VoiceInterviewEvaluationService._value(
            item, "questionIndex", "question_index"
        )
        reference = references.get(question_index)
        answers.append(VoiceEvaluationAnswerDetail(
            question_index=question_index,
            question=item.get("question", ""),
            category=item.get("category", ""),
            user_answer=VoiceInterviewEvaluationService._value(
                item, "userAnswer", "user_answer"
            ),
            score=item.get("score", 0),
            feedback=item.get("feedback", ""),
            reference_answer=(
                VoiceInterviewEvaluationService._value(
                    reference, "referenceAnswer", "reference_answer"
                ) if reference else None
            ),
            key_points=(
                VoiceInterviewEvaluationService._value(
                    reference, "keyPoints", "key_points"
                ) or [] if reference else []
            ),
        ))
      return VoiceEvaluationDetailDTO(
          session_id=entity.session_id,
          total_questions=len(answers),
          overall_score=entity.overall_score,
          overall_feedback=entity.overall_feedback,
          strengths=strengths,
          improvements=improvements,
          answers=answers,
      )
    except Exception as exc:
      raise BusinessException(
          ErrorCode.VOICE_EVALUATION_FAILED,
          f"构建评估结果失败: {exc}",
      ) from exc

  @staticmethod
  def _value(data: dict, camel_name: str, snake_name: str):
    return data.get(camel_name, data.get(snake_name))

  @staticmethod
  def _trim_to_none(text: str | None) -> str | None:
    if text is None or not text.strip():
      return None
    return text.strip()
