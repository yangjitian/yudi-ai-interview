import logging
import sys
from pathlib import Path

import structlog

from app.config.settings import get_settings


settings = get_settings()


def setup_logging() -> None:
  logging.basicConfig(
      format="%(message)s",
      stream=sys.stdout,
      level=logging.DEBUG if settings.app.app_debug else logging.INFO,
  )

  structlog.configure(
      processors=[
          structlog.contextvars.merge_contextvars,
          structlog.processors.add_log_level,
          structlog.processors.TimeStamper(fmt="iso"),
          structlog.processors.CallsiteParameterAdder(
              {
                  structlog.processors.CallsiteParameter.FILENAME,
                  structlog.processors.CallsiteParameter.FUNC_NAME,
                  structlog.processors.CallsiteParameter.LINENO,
              }
          ),
          structlog.processors.JSONRenderer(),
      ],
      wrapper_class=structlog.make_filtering_bound_logger(
          logging.DEBUG if settings.app.app_debug else logging.INFO
      ),
      context_class=dict,
      logger_factory=structlog.PrintLoggerFactory(),
      cache_logger_on_first_use=True,
  )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
  return structlog.get_logger(name)
