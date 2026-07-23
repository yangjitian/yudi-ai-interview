import logging
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

BEIJING_TZ = timezone(timedelta(hours=8))

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.errors import BusinessException, ErrorCode


log = logging.getLogger(__name__)

CHINESE_FONT_NAME = "ZhuqueFangsong"
CHINESE_FONT_PATH = (
    Path(__file__).resolve().parents[3]
    / "resources"
    / "fonts"
    / "ZhuqueFangsong-Regular.ttf"
)
HEADER_COLOR = colors.HexColor("#2980B9")
REFERENCE_COLOR = colors.HexColor("#27AE60")
MUTED_COLOR = colors.HexColor("#5F6B7A")


class PdfExportService:
  CATEGORY_LABEL_MAP: dict[str, str] = {
      "always_one": "项目实战",
      "core": "核心基础",
      "project": "项目实战",
      "general": "综合",
      "algorithm": "算法与数据结构",
      "system_design": "系统设计",
      "system_design_scenario": "系统设计/场景题",
      "frontend": "前端技术",
      "backend": "后端技术",
      "database": "数据库",
      "db_design": "数据库设计",
      "network": "计算机网络",
      "os": "操作系统",
      "net_os": "计网与操作系统",
      "distributed": "分布式",
      "cache": "缓存",
      "mq": "消息队列",
      "design_pattern": "设计模式",
      "high_availability": "高可用",
  }

  def __init__(self):
    self.font_name = self._register_chinese_font()
    self.styles = getSampleStyleSheet()
    for style_name in ("Normal", "Heading1", "Heading2"):
      self.styles[style_name].fontName = self.font_name
    self.interview_title_style = ParagraphStyle(
        "InterviewTitle",
        parent=self.styles["Heading1"],
        fontName=self.font_name,
        fontSize=24,
        leading=32,
        alignment=TA_CENTER,
        textColor=HEADER_COLOR,
        spaceAfter=12 * mm,
    )
    self.interview_section_style = ParagraphStyle(
        "InterviewSection",
        parent=self.styles["Heading2"],
        fontName=self.font_name,
        fontSize=15,
        leading=22,
        textColor=colors.white,
    )
    self.interview_body_style = ParagraphStyle(
        "InterviewBody",
        parent=self.styles["Normal"],
        fontName=self.font_name,
        fontSize=12.5,
        leading=22,
        spaceAfter=3.5 * mm,
        wordWrap="CJK",
    )
    self.interview_question_style = ParagraphStyle(
        "InterviewQuestion",
        parent=self.interview_body_style,
        fontSize=14,
        leading=24,
        textColor=HEADER_COLOR,
        spaceBefore=2 * mm,
        keepWithNext=True,
    )
    self.interview_feedback_style = ParagraphStyle(
        "InterviewFeedback",
        parent=self.interview_body_style,
        textColor=MUTED_COLOR,
    )
    self.interview_reference_style = ParagraphStyle(
        "InterviewReference",
        parent=self.interview_body_style,
        textColor=REFERENCE_COLOR,
    )
    self.interview_bullet_style = ParagraphStyle(
        "InterviewBullet",
        parent=self.interview_body_style,
        leftIndent=7 * mm,
        firstLineIndent=-7 * mm,
        spaceAfter=3 * mm,
    )

  def _register_chinese_font(self) -> str:
    if not CHINESE_FONT_PATH.is_file():
      log.error("未找到中文字体文件: %s", CHINESE_FONT_PATH)
      raise BusinessException(ErrorCode.EXPORT_PDF_FAILED, "字体文件缺失，请联系管理员")

    try:
      if CHINESE_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(CHINESE_FONT_NAME, str(CHINESE_FONT_PATH)))
      pdfmetrics.registerFontFamily(
          CHINESE_FONT_NAME,
          normal=CHINESE_FONT_NAME,
          bold=CHINESE_FONT_NAME,
          italic=CHINESE_FONT_NAME,
          boldItalic=CHINESE_FONT_NAME,
      )
      return CHINESE_FONT_NAME
    except Exception as e:
      log.error("注册中文字体失败: %s", e, exc_info=True)
      raise BusinessException(ErrorCode.EXPORT_PDF_FAILED, f"创建字体失败: {e}") from e

  def export_resume_analysis(
      self,
      filename: str,
      overall_score: int,
      content_score: int,
      structure_score: int,
      skill_match_score: int,
      expression_score: int,
      project_score: int,
      summary: str,
      strengths: list[str],
      suggestions: list[dict],
  ) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    story = []

    title_style = ParagraphStyle(
        "TitleStyle", parent=self.styles["Heading1"], fontSize=18, spaceAfter=20
    )
    story.append(Paragraph("简历分析报告", title_style))
    story.append(Spacer(1, 5 * mm))

    story.append(Paragraph(f"文件名: {self._safe_text(filename)}", self.styles["Normal"]))
    story.append(Paragraph(f"生成时间: {datetime.now(tz=BEIJING_TZ).strftime('%Y-%m-%d %H:%M')}", self.styles["Normal"]))
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph("一、综合评分", self.styles["Heading2"]))
    total_score = overall_score
    score_color = self._score_color(overall_score)
    story.append(
        Paragraph(
            f'<font color="{score_color}"><b>总分: {overall_score}/100</b></font>',
            self.styles["Normal"],
        )
    )
    story.append(Spacer(1, 5 * mm))

    score_data = [
        ["评分维度", "得分", "满分"],
        ["内容完整性", content_score, 25],
        ["结构清晰度", structure_score, 20],
        ["技能匹配度", skill_match_score, 25],
        ["表达专业性", expression_score, 15],
        ["项目经验", project_score, 15],
    ]
    score_table = Table(score_data, colWidths=[80 * mm, 40 * mm, 40 * mm])
    score_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4A90D9")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, -1), self.font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F5F5")]),
            ]
        )
    )
    story.append(score_table)
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph("二、AI 摘要", self.styles["Heading2"]))
    story.append(Paragraph(self._safe_text(summary), self.styles["Normal"]))
    story.append(Spacer(1, 8 * mm))

    if strengths:
      story.append(Paragraph("三、优势", self.styles["Heading2"]))
      for s in strengths[:8]:
        story.append(Paragraph(f"  {self._safe_text(s)}", self.styles["Normal"]))
      story.append(Spacer(1, 8 * mm))

    if suggestions:
      story.append(Paragraph("四、改进建议", self.styles["Heading2"]))
      for i, sg in enumerate(suggestions[:8], 1):
        category = self._safe_text(sg.get("category", ""))
        priority = self._safe_text(sg.get("priority", ""))
        issue = self._safe_text(sg.get("issue", ""))
        recommendation = self._safe_text(sg.get("recommendation", ""))
        story.append(
            Paragraph(
                f"<b>{i}. [{category}] {issue}</b> (优先级: {priority})",
                self.styles["Normal"],
            )
        )
        story.append(
            Paragraph(f"  建议: {recommendation}", self.styles["Normal"]
            )
        )
        story.append(Spacer(1, 3 * mm))

    doc.build(story)
    return buffer.getvalue()

  def export_interview_report(
      self,
      skill_id: str,
      overall_score: int,
      qa_details: list[dict],
      strengths: list[str],
      improvements: list[str],
      session_id: str = "",
      total_questions: int | None = None,
      status: str = "",
      created_at: datetime | None = None,
      completed_at: datetime | None = None,
      overall_feedback: str = "",
  ) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=25 * mm,
        bottomMargin=20 * mm,
    )

    story = []
    story.append(Paragraph("模拟面试报告", self.interview_title_style))

    story.append(self._section_header("面试信息"))
    if session_id:
      story.append(
          Paragraph(f"会话ID: {self._safe_text(session_id)}", self.interview_body_style)
      )
    story.append(
        Paragraph(f"面试方向: {self._safe_text(skill_id)}", self.interview_body_style)
    )
    story.append(
        Paragraph(
            f"题目数量: {total_questions if total_questions is not None else len(qa_details)}",
            self.interview_body_style,
        )
    )
    if status:
      story.append(
          Paragraph(f"面试状态: {self._status_text(status)}", self.interview_body_style)
      )
    if created_at:
      story.append(
          Paragraph(
              f"开始时间: {self._format_datetime(created_at)}",
              self.interview_body_style,
          )
      )
    if completed_at:
      story.append(
          Paragraph(
              f"完成时间: {self._format_datetime(completed_at)}",
              self.interview_body_style,
          )
      )

    story.append(self._section_header("综合评分"))
    story.append(Spacer(1, 2 * mm))
    story.append(self._score_box(overall_score))
    story.append(Spacer(1, 4 * mm))

    if overall_feedback:
      story.append(self._section_header("总体评价"))
      story.append(
          Paragraph(self._safe_text(overall_feedback), self.interview_body_style)
      )

    if strengths:
      story.append(self._section_header("表现优势"))
      for strength in strengths:
        story.append(
            Paragraph(f"• {self._safe_text(strength)}", self.interview_bullet_style)
        )

    if improvements:
      story.append(self._section_header("改进建议"))
      for improvement in improvements:
        story.append(
            Paragraph(f"• {self._safe_text(improvement)}", self.interview_bullet_style)
        )

    if qa_details:
      story.append(self._section_header("问答详情"))

    for i, qa in enumerate(qa_details, 1):
      if i > 1:
        story.append(self._question_divider())

      question_index = qa.get("question_index")
      question_number = question_index + 1 if isinstance(question_index, int) else i
      category = self._safe_text(self._category_label(qa.get("category")))

      score = qa.get("score")
      if isinstance(score, (int, float)):
        score_text = f"{score:g}/100"
        score_color = self._score_color(int(score))
      else:
        score_text = "未评分"
        score_color = "#7F8C8D"

      header_group = [
          Paragraph(
              f"<u><b>Q{question_number}. [{category}]</b></u>",
              self.interview_question_style,
          ),
          Paragraph(
              f"<b>题目：</b>{self._safe_text(qa.get('question') or '题目内容缺失')}",
              self.interview_body_style,
          ),
          Paragraph(
              f"<b>作答：</b>{self._safe_text(qa.get('user_answer') or '（未作答）')}",
              self.interview_body_style,
          ),
          Paragraph(
              f'<font color="{score_color}"><b>得分：{score_text}</b></font>',
              self.interview_body_style,
          ),
      ]
      story.append(KeepTogether(header_group))

      feedback = qa.get("feedback")
      if feedback:
        story.append(KeepTogether([
            Paragraph(
                f"<b>评价：</b>{self._safe_text(feedback)}",
                self.interview_feedback_style,
            ),
        ]))

      reference_answer = qa.get("reference_answer")
      if reference_answer:
        story.append(KeepTogether([
            Paragraph(
                "<b>参考答案：</b>",
                ParagraphStyle(
                    "RefLabel",
                    parent=self.interview_reference_style,
                    keepWithNext=True,
                ),
            ),
            Paragraph(
                self._safe_text(reference_answer),
                self.interview_reference_style,
            ),
        ]))

    doc.build(
        story,
        onFirstPage=self._draw_interview_page,
        onLaterPages=self._draw_interview_page,
    )
    return buffer.getvalue()

  def _draw_interview_page(self, canvas, doc) -> None:
    canvas.saveState()
    canvas.setTitle("模拟面试报告")
    canvas.setAuthor("AI Interview")

    canvas.setStrokeColor(HEADER_COLOR)
    canvas.setLineWidth(1.5)
    canvas.line(20 * mm, A4[1] - 14 * mm, A4[0] - 20 * mm, A4[1] - 14 * mm)
    canvas.setFont(self.font_name, 9)
    canvas.setFillColor(HEADER_COLOR)
    canvas.drawString(20 * mm, A4[1] - 12 * mm, "模拟面试报告")
    canvas.drawRightString(A4[0] - 20 * mm, A4[1] - 12 * mm, "AI Interview Platform")

    canvas.setFont(self.font_name, 8)
    canvas.setFillColor(colors.HexColor("#95A5A6"))
    canvas.drawCentredString(A4[0] / 2, 10 * mm, f"第 {doc.page} 页")
    canvas.restoreState()

  def _section_header(self, title: str) -> Table:
    cell = Paragraph(f"<b>{self._safe_text(title)}</b>", self.interview_section_style)
    table = Table([[cell]], colWidths=[A4[0] - 40 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HEADER_COLOR),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    table.spaceBefore = 4 * mm
    table.spaceAfter = 3 * mm
    table.keepWithNext = True
    return table

  def _score_box(self, overall_score: int) -> Table:
    score_color = colors.HexColor(self._score_color(overall_score))
    score_cell = Paragraph(
        f"<b>{overall_score}</b>",
        ParagraphStyle(
            "ScoreNumber",
            fontName=self.font_name,
            fontSize=36,
            leading=44,
            textColor=score_color,
            alignment=TA_CENTER,
        ),
    )
    label_cell = Paragraph(
        "综合得分 / 100",
        ParagraphStyle(
            "ScoreLabel",
            fontName=self.font_name,
            fontSize=12,
            leading=16,
            textColor=MUTED_COLOR,
            alignment=TA_CENTER,
        ),
    )
    table = Table([[score_cell], [label_cell]], colWidths=[A4[0] - 40 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8F9FA")),
                ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#DEE2E6")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]
        )
    )
    return table

  def _question_divider(self) -> HRFlowable:
    return HRFlowable(
        width="100%",
        thickness=0.5,
        color=colors.HexColor("#D5D8DC"),
        spaceAfter=3 * mm,
        spaceBefore=3 * mm,
    )

  def _status_text(self, status: str) -> str:
    return {
        "CREATED": "已创建",
        "IN_PROGRESS": "进行中",
        "COMPLETED": "已完成",
        "EVALUATED": "已评估",
    }.get(status, self._safe_text(status))

  def _category_label(self, category: str | None) -> str:
    if not category:
      return "综合"

    value = str(category).strip()
    chinese_followup = re.fullmatch(r"(.+?)[（(]\s*追问\s*(\d*)\s*[）)]", value)
    if chinese_followup:
      base_label = self._category_label(chinese_followup.group(1))
      number = chinese_followup.group(2)
      return f"{base_label}（追问{number}）"

    english_followup = re.fullmatch(
        r"(.+?)[_\-\s]*(?:follow_?up)[_\-\s]*(\d*)",
        value,
        flags=re.IGNORECASE,
    )
    if english_followup:
      base_label = self._category_label(english_followup.group(1))
      number = english_followup.group(2)
      return f"{base_label}（追问{number}）"

    if any("\u4e00" <= char <= "\u9fff" for char in value):
      return value
    return self.CATEGORY_LABEL_MAP.get(value.lower(), value)

  def _format_datetime(self, value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")

  def _score_color(self, score: int) -> str:
    if score >= 80:
      return "#28A745"
    elif score >= 60:
      return "#FFC107"
    else:
      return "#DC3545"

  def _safe_text(self, text: str | None) -> str:
    if not text:
      return ""
    cleaned = "".join(
        char
        for char in str(text)
        if ord(char) <= 0xFFFF
        and unicodedata.category(char) not in {"So", "Cs"}
        and (unicodedata.category(char) != "Cc" or char in "\n\r\t")
    )
    normalized = cleaned.strip().replace("\r\n", "\n").replace("\r", "\n")
    return escape(normalized).replace("\n", "<br/>")
