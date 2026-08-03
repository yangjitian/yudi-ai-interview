import asyncio
import logging

from app.config.database import _async_session_factory
from app.infrastructure.redis.voice_evaluate_producer import VoiceEvaluateStreamProducer
from app.repositories.voice_evaluation_repository import VoiceEvaluationRepository
from app.repositories.voice_message_repository import VoiceMessageRepository
from app.repositories.voice_session_repository import VoiceSessionRepository
from app.services.voice.session_service import VoiceInterviewSessionService

log = logging.getLogger(__name__)


class VoiceInterviewCleanupScheduler:
  def __init__(self, interval_seconds: int = 60):
    self.interval_seconds = interval_seconds
    self._running = False
    self._task: asyncio.Task[None] | None = None

  async def start(self) -> None:
    if self._task and not self._task.done():
      return
    self._running = True
    self._task = asyncio.create_task(self._run_loop())

  async def stop(self) -> None:
    self._running = False
    if self._task is None:
      return
    self._task.cancel()
    try:
      await self._task
    except asyncio.CancelledError:
      pass
    self._task = None

  async def run_once(self) -> int:
    async with _async_session_factory() as db:
      service = VoiceInterviewSessionService(
          session_repo=VoiceSessionRepository(db),
          message_repo=VoiceMessageRepository(db),
          evaluation_repo=VoiceEvaluationRepository(db),
          evaluate_producer=VoiceEvaluateStreamProducer(),
      )
      return await service.cleanup_stale_sessions()

  async def _run_loop(self) -> None:
    while self._running:
      try:
        cleaned = await self.run_once()
        if cleaned:
          log.info("Stale voice interview sessions cleaned: %d", cleaned)
      except asyncio.CancelledError:
        raise
      except Exception:
        log.exception("Error during stale voice interview session cleanup")
      await asyncio.sleep(self.interval_seconds)
