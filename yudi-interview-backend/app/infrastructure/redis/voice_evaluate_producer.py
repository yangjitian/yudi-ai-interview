import logging

from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.stream_constants import (
    FIELD_RETRY_COUNT,
    FIELD_VOICE_SESSION_ID,
    STREAM_MAX_LEN,
    VOICE_EVALUATE_STREAM_KEY,
)

log = logging.getLogger(__name__)


class VoiceEvaluateStreamProducer:
  def __init__(self, redis_client=None):
    self.redis_client = redis_client

  async def send_evaluate_task(self, session_id: int) -> bool:
    try:
      client = self.redis_client or await get_redis()
      await client.xadd(
          VOICE_EVALUATE_STREAM_KEY,
          {
              FIELD_VOICE_SESSION_ID: str(session_id),
              FIELD_RETRY_COUNT: "0",
          },
          maxlen=STREAM_MAX_LEN,
          approximate=True,
      )
      log.info("语音面试评估任务已发送: sessionId=%s", session_id)
      return True
    except Exception:
      log.exception("发送语音面试评估任务失败: sessionId=%s", session_id)
      return False
