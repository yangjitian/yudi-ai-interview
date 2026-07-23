import hashlib
from pathlib import Path


def compute_sha256(content: bytes | bytearray | memoryview) -> str:
  h = hashlib.sha256()
  h.update(content)
  return h.hexdigest()


async def compute_file_hash(file_path: Path) -> str:
  h = hashlib.sha256()
  with open(file_path, "rb") as f:
    for chunk in iter(lambda: f.read(8192), b""):
      h.update(chunk)
  return h.hexdigest()
