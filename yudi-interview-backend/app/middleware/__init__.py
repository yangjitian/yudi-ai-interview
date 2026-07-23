from fastapi import FastAPI

from app.config.settings import get_settings
from app.middleware.rate_limit import RateLimitMiddleware


def setup_middleware(app: FastAPI) -> None:
  settings = get_settings()
  app.add_middleware(RateLimitMiddleware)
