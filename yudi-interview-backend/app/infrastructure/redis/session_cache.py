import json
import logging
from typing import Optional

from app.infrastructure.redis.client import get_redis
from app.models.interview_dto import InterviewQuestionDTO, InterviewSessionDTO


log = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 86400


class SessionCache:
  def __init__(self, ttl: int = SESSION_TTL_SECONDS):
    self.ttl = ttl

  async def save_session(
      self,
      session_id: str,
      resume_text: str,
      resume_id: int | None,
      questions: list[InterviewQuestionDTO],
      current_index: int,
      status: str,
      is_fallback: bool = False,
      fallback_reason: str | None = None,
      generation_mode: str = "llm",
  ) -> None:
    redis_client = await get_redis()
    key = f"interview:session:{session_id}"
    data = {
        "session_id": session_id,
        "resume_text": resume_text,
        "resume_id": str(resume_id) if resume_id else "",
        "questions": json.dumps([q.model_dump() for q in questions], ensure_ascii=False),
        "current_index": str(current_index),
        "status": status,
        "is_fallback": "1" if is_fallback else "0",
        "fallback_reason": fallback_reason or "",
        "generation_mode": generation_mode,
    }
    await redis_client.hset(key, mapping=data)
    await redis_client.expire(key, self.ttl)
    log.info("会话已缓存: sessionId=%s", session_id)

  async def get_session(self, session_id: str) -> Optional[InterviewSessionDTO]:
    redis_client = await get_redis()
    key = f"interview:session:{session_id}"
    data = await redis_client.hgetall(key)
    if not data:
      return None

    questions = []
    if data.get("questions"):
      try:
        questions = [
            InterviewQuestionDTO(**q)
            for q in json.loads(data["questions"])
        ]
      except Exception as e:
        log.warning("解析缓存问题失败: %s", e)

    resume_text = data.get("resume_text", "")
    resume_id_str = data.get("resume_id", "")
    resume_id = int(resume_id_str) if resume_id_str else None

    return InterviewSessionDTO(
        session_id=session_id,
        resume_text=resume_text,
        total_questions=len(questions),
        current_index=int(data.get("current_index", 0)),
        questions=questions,
        status=data.get("status", "CREATED"),
        is_fallback=data.get("is_fallback") == "1",
        fallback_reason=data.get("fallback_reason") or None,
        generation_mode=data.get("generation_mode") or "llm",
    )

  async def update_status(self, session_id: str, status: str) -> None:
    redis_client = await get_redis()
    key = f"interview:session:{session_id}"
    await redis_client.hset(key, "status", status)
    await redis_client.expire(key, self.ttl)

  async def update_current_index(self, session_id: str, index: int) -> None:
    redis_client = await get_redis()
    key = f"interview:session:{session_id}"
    await redis_client.hset(key, "current_index", str(index))
    await redis_client.expire(key, self.ttl)

  async def update_questions(
      self, session_id: str, questions: list[InterviewQuestionDTO]
  ) -> None:
    redis_client = await get_redis()
    key = f"interview:session:{session_id}"
    await redis_client.hset(
        key, "questions", json.dumps([q.model_dump() for q in questions], ensure_ascii=False)
    )
    await redis_client.expire(key, self.ttl)

  async def delete_session(self, session_id: str) -> None:
    redis_client = await get_redis()
    key = f"interview:session:{session_id}"
    await redis_client.delete(key)

  async def refresh_session_ttl(self, session_id: str) -> None:
    redis_client = await get_redis()
    key = f"interview:session:{session_id}"
    await redis_client.expire(key, self.ttl)

  async def find_unfinished_session_id(self, resume_id: int) -> Optional[str]:
    return None
