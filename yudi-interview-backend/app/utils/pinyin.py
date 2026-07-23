from pypinyin import lazy_pinyin


def to_pascal_case_pinyin(text: str) -> str:
  py_words = lazy_pinyin(text)
  return "".join(w.capitalize() for w in py_words if w)
