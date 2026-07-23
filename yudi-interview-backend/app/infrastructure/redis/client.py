import os
from pathlib import Path

import redis.asyncio as redis

from app.config.settings import get_settings


settings = get_settings()

_redis_client: redis.Redis | None = None
_rate_limit_script: str | None = None


async def get_redis() -> redis.Redis:
  global _redis_client
  if _redis_client is None:
    _redis_client = redis.from_url(
        settings.redis.url,
        encoding="utf-8",
        decode_responses=True,
    )
  return _redis_client


async def close_redis() -> None:
  global _redis_client
  if _redis_client is not None:
    await _redis_client.aclose()
    _redis_client = None


async def get_rate_limit_script() -> str:
  global _rate_limit_script
  if _rate_limit_script is None:
    script_path = _get_script_path()
    _rate_limit_script = script_path.read_text(encoding="utf-8")
  return _rate_limit_script


def _get_script_path() -> Path:
  import os
  backend_dir = Path(__file__).resolve().parent.parent.parent
  script_path = backend_dir / "scripts" / "rate_limit_single.lua"
  if script_path.exists():
    return script_path
  java_scripts = (
    Path(os.environ.get("JAVA_RESOURCES", ""))
    / "scripts" / "rate_limit_single.lua"
  )
  if java_scripts.exists():
    return java_scripts
  return backend_dir / "scripts" / "rate_limit_single.lua"
