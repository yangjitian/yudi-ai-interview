import re
from pathlib import Path


_ALLOWED_RESUME_TYPES = {
  "application/pdf",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "text/plain",
}

_ALLOWED_RESUME_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}

_ALLOWED_KB_TYPES = {
  "application/pdf",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "text/plain",
  "text/markdown",
}

_ALLOWED_KB_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".md"}


def is_resume_allowed(content_type: str | None, extension: str | None) -> bool:
  if content_type in _ALLOWED_RESUME_TYPES:
    return True
  if extension and extension.lower() in _ALLOWED_RESUME_EXTENSIONS:
    return True
  return False


def is_knowledge_base_allowed(content_type: str | None, extension: str | None) -> bool:
  if content_type in _ALLOWED_KB_TYPES:
    return True
  if extension and extension.lower() in _ALLOWED_KB_EXTENSIONS:
    return True
  return False


def get_extension_from_filename(filename: str) -> str:
  return Path(filename).suffix.lower()
