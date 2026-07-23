import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from app.infrastructure.ai.provider_registry import get_plain_chat_client
from app.infrastructure.ai.structured_output import StructuredOutputInvoker
from app.infrastructure.ai.prompt_security import sanitize


log = logging.getLogger(__name__)


class ScoreDetail(BaseModel):
  content_score: int = 0
  structure_score: int = 0
  skill_match_score: int = 0
  expression_score: int = 0
  project_score: int = 0


class SuggestionItem(BaseModel):
  category: str
  priority: str
  issue: str
  recommendation: str


class ResumeAnalysisResponse(BaseModel):
  overall_score: int = 0
  score_detail: ScoreDetail = ScoreDetail()
  summary: str = ""
  strengths: list[str] = []
  suggestions: list[SuggestionItem] = []
  resume_text: str = ""


class ResumeGradingService:
  def __init__(self):
    self.structured_invoker = StructuredOutputInvoker()

  async def analyze_resume(
      self,
      resume_text: str,
      provider_id: str | None = None,
  ) -> ResumeAnalysisResponse:
    log.info("开始分析简历，文本长度: %d 字符", len(resume_text))

    sanitized_text = sanitize(resume_text)

    system_prompt = (
        "你是一个专业的简历评审专家，擅长评估求职者的简历质量。\n"
        "请根据简历内容提供客观、全面的评估，包括总分、各维度评分、优势和待改进建议。\n"
        "你的回答必须严格遵循以下JSON格式，不得添加任何额外说明。"
    )

    user_prompt = (
        f"请分析以下简历并评估：\n\n{sanitized_text}\n\n"
        "评分标准（总分100分）：\n"
        "- 内容完整性（25分）：信息是否完整、相关性强\n"
        "- 结构清晰度（20分）：排版是否规范、层次是否分明\n"
        "- 技能匹配度（25分）：技术栈是否突出、与职位契合度\n"
        "- 表达专业性（15分）：措辞是否准确、简洁、专业\n"
        "- 项目经验（15分）：项目描述是否具体、有深度\n"
    )

    try:
      chat_client = await get_plain_chat_client(provider_id)

      result = await self.structured_invoker.invoke(
          chat_model=chat_client,
          system_prompt=system_prompt,
          user_prompt=user_prompt,
          output_schema=ResumeAnalysisResponse,
          error_code=None,
          error_prefix="简历分析失败：",
          operation_name="简历分析",
      )

      log.info("简历分析完成，总分: %d", result.overall_score)
      return result

    except Exception as e:
      log.error("简历分析AI调用失败: %s", e)
      return self._create_error_response(sanitized_text, str(e))

  def _create_error_response(
      self, resume_text: str, error_message: str
  ) -> ResumeAnalysisResponse:
    return ResumeAnalysisResponse(
        overall_score=0,
        score_detail=ScoreDetail(),
        summary=f"分析过程中出现错误: {error_message}",
        strengths=[],
        suggestions=[
            SuggestionItem(
                category="系统",
                priority="高",
                issue="AI分析服务暂时不可用",
                recommendation="请稍后重试，或检查AI服务是否正常运行",
            )
        ],
        resume_text=resume_text,
    )
