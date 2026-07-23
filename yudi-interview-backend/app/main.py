import asyncio
import logging
from contextlib import suppress
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config.logging import setup_logging
from app.config.cors import setup_cors
from app.config.database import init_db, close_db
from app.infrastructure.redis.client import get_redis, close_redis
from app.infrastructure.ai.provider_registry import (
    init_llm_clients,
    reload as reload_provider_registry,
    shutdown_llm_clients,
)
from app.services.llm.bootstrap import initialize_builtin_providers
from app.core.exception_handlers import register_exception_handlers


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logging.getLogger("uvicorn").info("Starting AI Interview Platform...")
    await init_db()
    await initialize_builtin_providers()
    await reload_provider_registry()
    await get_redis()
    # Task 1: 启动时预热 LLM 客户端连接池
    await init_llm_clients()
    await _start_background_consumers()
    await _start_schedule_status_updater()
    yield
    await _stop_schedule_status_updater()
    await _stop_background_consumers()
    # Task 1: 关闭时清理 LLM 客户端连接池
    await shutdown_llm_clients()
    await close_redis()
    await close_db()
    logging.getLogger("uvicorn").info("AI Interview Platform stopped.")


_consumers = []
_schedule_status_task: asyncio.Task | None = None


async def _start_background_consumers() -> None:
  global _consumers

  try:
    from app.infrastructure.redis.analyze_consumer import AnalyzeStreamConsumer
    from app.infrastructure.redis.evaluate_producer import EvaluateStreamConsumer
    from app.infrastructure.redis.vectorize_producer import VectorizeStreamConsumer
    from app.infrastructure.redis.voice_evaluate_producer import VoiceEvaluateStreamConsumer

    _consumers = [
        AnalyzeStreamConsumer(),
        EvaluateStreamConsumer(),
        VectorizeStreamConsumer(),
        VoiceEvaluateStreamConsumer(),
    ]
    for c in _consumers:
      await c.start()
    logging.getLogger(__name__).info(
        "Started %d background consumers", len(_consumers)
    )
  except Exception as e:
    logging.getLogger(__name__).exception(
        "Failed to start background consumers: %s", e
    )
    await _stop_background_consumers()
    _consumers = []
    raise


async def _stop_background_consumers() -> None:
  for c in _consumers:
    try:
      await c.stop()
    except Exception:
      pass


async def _start_schedule_status_updater() -> None:
  global _schedule_status_task
  _schedule_status_task = asyncio.create_task(_schedule_status_update_loop())


async def _stop_schedule_status_updater() -> None:
  global _schedule_status_task
  if _schedule_status_task is None:
    return
  _schedule_status_task.cancel()
  with suppress(asyncio.CancelledError):
    await _schedule_status_task
  _schedule_status_task = None


async def _schedule_status_update_loop() -> None:
  from app.config.database import get_db
  from app.repositories.schedule_repository import ScheduleRepository
  from app.services.schedule.service import InterviewScheduleService

  while True:
    try:
      async for db in get_db():
        updated = await InterviewScheduleService(
            ScheduleRepository(db)
        ).update_expired()
        if updated:
          logging.getLogger(__name__).info(
              "已将 %d 条过期面试标记为已取消", updated
          )
    except Exception:
      logging.getLogger(__name__).exception("更新过期面试状态失败")
    await asyncio.sleep(3600)


def create_app() -> FastAPI:
  app = FastAPI(
      title="AI Interview Platform",
      version="1.0.0",
      description="Yudi AI Interview Python Backend - FastAPI + LangChain",
      lifespan=lifespan,
  )

  setup_cors(app)
  register_exception_handlers(app)

  app.include_router(_import_resume_router(), tags=["简历"])
  app.include_router(_import_interview_router(), tags=["面试"])
  app.include_router(_import_kb_router(), tags=["知识库"])
  app.include_router(_import_knowledge_router(), tags=["知识库管理"])
  app.include_router(_import_rag_chat_router(), tags=["RAG 聊天"])
  app.include_router(_import_schedule_router(), tags=["面试日程"])
  app.include_router(_import_voice_router(), prefix="/api/voice", tags=["语音面试"])
  app.include_router(_import_voice_router(), prefix="/api/voice-interview", tags=["语音面试"])
  app.include_router(_import_voice_ws_router(), prefix="/api/voice", tags=["语音面试"])
  app.include_router(_import_llm_admin_router(), tags=["LLM 管理"])
  app.include_router(_import_skills_router(), tags=["技能管理"])

  @app.get("/", response_model=None)
  async def root() -> dict:
    from app.core.result import ApiResponse
    return ApiResponse.success({"status": "ok"})

  @app.get("/health", response_model=None)
  async def health() -> dict:
    from app.core.result import ApiResponse
    return ApiResponse.success({"status": "UP"})

  return app


def _import_resume_router():
  from app.routers.resume import router
  return router

def _import_interview_router():
  from app.routers.interview import router
  return router

def _import_kb_router():
  from app.routers.knowledge_base import router, _alias_router
  return router

def _import_knowledge_router():
  from app.routers.knowledge_base import _alias_router
  return _alias_router

def _import_rag_chat_router():
  from app.routers.rag_chat import router
  return router

def _import_schedule_router():
  from app.routers.schedule import router
  return router

def _import_voice_router():
  from app.routers.voice_interview import router
  return router

def _import_voice_ws_router():
  from app.routers.voice_interview import ws_router
  return ws_router

def _import_llm_admin_router():
  from app.routers.llm_admin import router
  return router

def _import_skills_router():
  from app.routers.skills import router
  return router


app = create_app()
