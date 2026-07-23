import re


_SEMANTIC_NOISE_PATTERNS = [
    re.compile(r"!\[.*?\]\(.*?\)"),
    re.compile(r"\[.*?\]\(.*?\)"),
    re.compile(r"https?://\S+"),
    re.compile(r"file://\S+"),
    re.compile(r"[A-Za-z]:[\\\/][^\s]+"),
    re.compile(r"={3,}"),
    re.compile(r"-{3,}"),
    re.compile(r"\*{3,}"),
    re.compile(r"_{3,}"),
    re.compile(r"\.{3,}"),
]

_NORMALIZE_PATTERNS = [
    (re.compile(r"\r\n"), "\n"),
    (re.compile(r"\r"), "\n"),
    (re.compile(r"[ \t]+\n"), "\n"),
    (re.compile(r"\n{3,}"), "\n\n"),
    (re.compile(r"[ \t]+$"), ""),
]


def clean(text: str) -> str:
  for pattern in _SEMANTIC_NOISE_PATTERNS:
    text = pattern.sub("", text)
  text = _collapse_whitespace(text)
  return text.strip()


def clean_to_single_line(text: str) -> str:
  text = clean(text)
  text = re.sub(r"\s+", " ", text)
  return text.strip()


def _collapse_whitespace(text: str) -> str:
  for pattern, replacement in _NORMALIZE_PATTERNS:
    text = pattern.sub(replacement, text)
  return text


def strip_html(html: str) -> str:
  text = re.sub(r"<[^>]+>", "", html)
  return clean(text)
