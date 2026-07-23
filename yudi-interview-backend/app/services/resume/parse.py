import logging
from datetime import datetime, timezone
from typing import Any

from app.infrastructure.parser.document_parser import parse_document
from app.utils.file_validation import get_extension_from_filename, is_resume_allowed
from app.core.errors import BusinessException, ErrorCode


log = logging.getLogger(__name__)


class ResumeParseService:
  async def parse_resume(
      self, content: bytes, content_type: str | None, filename: str
  ) -> str:
    text = await parse_document(content, content_type, filename)
    if not text or not text.strip():
      raise BusinessException(
          ErrorCode.RESUME_PARSE_FAILED,
          "无法从文件中提取文本内容，请确保文件不是扫描版PDF",
      )
    log.info("简历解析完成: %s, 文本长度: %d 字符", filename, len(text))
    return text

  def validate_content_type(self, content_type: str | None) -> None:
    ext = ""  # unknown extension fallback
    if not is_resume_allowed(content_type, ext):
      raise BusinessException(
          ErrorCode.RESUME_FILE_TYPE_NOT_SUPPORTED,
          f"不支持的文件类型: {content_type}",
      )
