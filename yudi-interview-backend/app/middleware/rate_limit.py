from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse
from uuid import uuid4

from app.config.settings import get_settings
from app.core.errors import ErrorCode
from app.core.result import ApiResponse
from app.infrastructure.redis.client import get_redis, get_rate_limit_script


settings = get_settings()

RATE_LIMIT_SCRIPT_SHA: str | None = None


async def _load_script(redis_client) -> str:
  global RATE_LIMIT_SCRIPT_SHA
  if RATE_LIMIT_SCRIPT_SHA is None:
    script = await get_rate_limit_script()
    RATE_LIMIT_SCRIPT_SHA = await redis_client.script_load(script)
  return RATE_LIMIT_SCRIPT_SHA


def _extract_client_ip(request: Request) -> str:
  forwarded = request.headers.get("x-forwarded-for")
  if forwarded:
    return forwarded.split(",")[0].strip()
  real_ip = request.headers.get("x-real-ip")
  if real_ip:
    return real_ip.strip()
  if request.client:
    return request.client.host
  return "unknown"


def _extract_user_id(request: Request) -> str:
  user_id = getattr(request.state, "user_id", None)
  if user_id:
    return str(user_id)
  header_uid = request.headers.get("x-user-id")
  if header_uid:
    return header_uid
  return "anonymous"


class RateLimitConfig:
  def __init__(
      self,
      count: int,
      interval_seconds: int,
      dimension: str = "IP",
  ):
    self.count = count
    self.interval_ms = interval_seconds * 1000
    self.dimension = dimension


async def check_rate_limit(
    request: Request,
    key_prefix: str,
    config: RateLimitConfig,
) -> tuple[bool, int]:
  redis_client = await get_redis()

  ip = _extract_client_ip(request)
  user_id = _extract_user_id(request)

  if config.dimension == "GLOBAL":
    dim_key = "global"
  elif config.dimension == "USER":
    dim_key = f"user:{user_id}"
  else:
    dim_key = f"ip:{ip}"

  redis_key = f"ratelimit:{key_prefix}:{dim_key}"
  now_ms = await redis_client.time()
  current_time = int(now_ms[0]) * 1000 + int(now_ms[1]) / 1000

  try:
    sha = await _load_script(redis_client)
    result = await redis_client.evalsha(
        sha,
        1,
        redis_key,
        str(int(current_time)),
        "1",
        str(config.interval_ms),
        str(config.count),
        str(uuid4()),
    )
    result_code = int(result)
    return result_code == 1, result_code
  except Exception:
    return True, config.count


class RateLimitMiddleware(BaseHTTPMiddleware):
  async def dispatch(
      self, request: Request, call_next: RequestResponseEndpoint
  ) -> JSONResponse:
    route_path = request.url.path

    route_limits = _get_route_limits(route_path, request.method)
    if not route_limits:
      return await call_next(request)

    redis_client = await get_redis()
    try:
      for limit_config in route_limits:
        allowed, remaining = await check_rate_limit(
            request,
            f"{request.app.title}:{route_path}:{request.method}",
            limit_config,
        )
        if not allowed:
          return JSONResponse(
              status_code=200,
              content=ApiResponse.error(
                  ErrorCode.RATE_LIMIT_EXCEEDED.code,
                  ErrorCode.RATE_LIMIT_EXCEEDED.message,
              ).model_dump(),
          )
    except Exception:
      pass

    response = await call_next(request)
    return response


_ROUTE_LIMITS: dict[str, list[RateLimitConfig]] = {
    "/api/resumes/upload": [
        RateLimitConfig(count=5, interval_seconds=60, dimension="GLOBAL"),
        RateLimitConfig(count=5, interval_seconds=60, dimension="IP"),
    ],
    "/api/resumes/{resume_id}/reanalyze": [
        RateLimitConfig(count=2, interval_seconds=60, dimension="IP"),
    ],
}


def _get_route_limits(path: str, method: str) -> list[RateLimitConfig]:
  matched = []
  for pattern, limits in _ROUTE_LIMITS.items():
    if _path_matches(pattern, path):
      matched.extend(limits)
  return matched


def _path_matches(pattern: str, path: str) -> bool:
  import re
  regex = pattern.replace("{id}", r"[^/]+").replace("{resume_id}", r"[^/]+")
  return bool(re.match(f"^{regex}$", path))
