import re
from typing import Any

from starlette.middleware.cors import CORSMiddleware

from app.config.settings import get_settings


def setup_cors(app: Any) -> None:
  settings = get_settings()

  origins = settings.app.cors_origins
  if isinstance(origins, str):
    origins = [o.strip() for o in re.split(r"[,;]", origins) if o.strip()]

  app.add_middleware(
      CORSMiddleware,
      allow_origins=origins or ["http://localhost:5173"],
      allow_credentials=True,
      allow_methods=["*"],
      allow_headers=["*"],
  )
