from app.core.errors import BusinessException, ErrorCode
from app.infrastructure.parser.document_parser import parse_document


class DocumentProcessor:
  async def extract_text(self, file_data: bytes, file_type: str) -> str:
    normalized_type = file_type.lower().lstrip(".")
    content_types = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "md": "text/markdown",
        "txt": "text/plain",
    }
    content_type = content_types.get(normalized_type)
    if content_type is None:
      raise BusinessException(
          ErrorCode.BAD_REQUEST, f"不支持的知识库文件类型: {file_type}"
      )
    text = await parse_document(
        file_data, content_type, f"document.{normalized_type}"
    )
    if not text.strip():
      raise BusinessException(ErrorCode.BAD_REQUEST, "文档中未提取到有效文本")
    return text

  def split_into_chunks(
      self,
      text: str,
      chunk_size: int = 500,
      chunk_overlap: int = 50,
  ) -> list[str]:
    if chunk_size <= 0 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
      raise ValueError("分块参数无效")

    normalized = text.strip()
    if not normalized:
      return []

    chunks: list[str] = []
    start = 0
    boundaries = "\n。！？；.!?;"
    while start < len(normalized):
      target_end = min(start + chunk_size, len(normalized))
      end = target_end
      if target_end < len(normalized):
        search_start = start + max(chunk_size // 2, 1)
        candidates = [normalized.rfind(mark, search_start, target_end) for mark in boundaries]
        boundary = max(candidates)
        if boundary >= search_start:
          end = boundary + 1

      chunk = normalized[start:end].strip()
      if chunk:
        chunks.append(chunk)
      if end >= len(normalized):
        break
      start = max(end - chunk_overlap, start + 1)
    return chunks
