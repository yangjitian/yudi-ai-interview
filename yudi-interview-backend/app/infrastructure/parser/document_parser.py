import logging
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from docx import Document

from app.utils.text_cleaning import clean


log = logging.getLogger(__name__)


async def parse_pdf(content: bytes) -> str:
  try:
    reader = PdfReader(BytesIO(content))
    texts = []
    for page in reader.pages:
      text = page.extract_text()
      if text:
        texts.append(text)
    return clean("\n".join(texts))
  except Exception as e:
    log.error("PDF parse failed: %s", e)
    raise


async def parse_docx(content: bytes) -> str:
  try:
    doc = Document(BytesIO(content))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return clean("\n".join(paragraphs))
  except Exception as e:
    log.error("DOCX parse failed: %s", e)
    raise


async def parse_txt(content: bytes) -> str:
  try:
    text = content.decode("utf-8", errors="replace")
    return clean(text)
  except Exception as e:
    log.error("TXT parse failed: %s", e)
    raise


async def parse_markdown(content: bytes) -> str:
  try:
    text = content.decode("utf-8", errors="replace")
    return clean(text)
  except Exception as e:
    log.error("Markdown parse failed: %s", e)
    raise


async def parse_document(content: bytes, content_type: str | None, filename: str) -> str:
  ext = Path(filename).suffix.lower()
  if content_type == "application/pdf" or ext == ".pdf":
    return await parse_pdf(content)
  elif (
      content_type
      == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
      or ext == ".docx"
  ):
    return await parse_docx(content)
  elif (
      content_type == "application/msword" or ext == ".doc"
  ):
    return await parse_docx(content)
  elif ext == ".md":
    return await parse_markdown(content)
  else:
    return await parse_txt(content)
