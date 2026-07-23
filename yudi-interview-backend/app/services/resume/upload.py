import logging

from app.core.errors import BusinessException, ErrorCode
from app.infrastructure.storage.file_storage import generate_storage_key, upload_file, generate_public_url
from app.models.resume import ResumeEntity
from app.models.common import AsyncTaskStatus
from app.repositories.resume_repository import ResumeRepository
from app.services.resume.parse import ResumeParseService


log = logging.getLogger(__name__)


class ResumeUploadService:
  def __init__(
      self,
      resume_repo: ResumeRepository,
      parse_service: ResumeParseService,
      storage_key_prefix: str = "resumes",
  ):
    self.resume_repo = resume_repo
    self.parse_service = parse_service
    self.storage_key_prefix = storage_key_prefix

  async def upload_and_analyze(
      self,
      content: bytes,
      filename: str,
      content_type: str | None,
      file_hash: str,
  ) -> dict:
    from app.infrastructure.redis.analyze_consumer import send_analyze_task

    file_size = len(content)
    log.info(
        "收到简历上传请求: %s, 大小: %d bytes", filename, file_size
    )

    existing = await self.resume_repo.find_by_hash(file_hash)
    if existing:
      log.info("检测到重复简历: resumeId=%d", existing.id)
      return {
          "resume_id": existing.id,
          "analyze_status": existing.analyze_status,
          "duplicate": True,
      }

    resume_text = await self.parse_service.parse_resume(content, content_type, filename)

    storage_key = generate_storage_key(self.storage_key_prefix, filename)
    await upload_file(content, storage_key, content_type)
    storage_url = generate_public_url(storage_key)

    entity = ResumeEntity(
        file_hash=file_hash,
        original_filename=filename,
        file_size=file_size,
        content_type=content_type,
        storage_key=storage_key,
        storage_url=storage_url,
        resume_text=resume_text,
        analyze_status=AsyncTaskStatus.PENDING.value,
    )
    saved = await self.resume_repo.save(entity)

    await send_analyze_task(saved.id, resume_text)

    log.info(
        "简历上传完成: resumeId=%d, fileKey=%s", saved.id, storage_key
    )
    return {
        "resume_id": saved.id,
        "analyze_status": AsyncTaskStatus.PENDING.value,
        "duplicate": False,
    }

  async def reanalyze(self, resume_id: int) -> None:
    from app.infrastructure.redis.analyze_consumer import send_analyze_task

    entity = await self.resume_repo.find_by_id(resume_id)
    if entity is None:
      raise BusinessException(ErrorCode.RESUME_NOT_FOUND, "简历不存在")

    resume_text = entity.resume_text
    if not resume_text or not resume_text.strip():
      raise BusinessException(
          ErrorCode.RESUME_PARSE_FAILED, "无法获取简历文本内容"
      )

    entity.analyze_status = AsyncTaskStatus.PENDING.value
    entity.analyze_error = None
    await self.resume_repo.save(entity)

    await send_analyze_task(resume_id, resume_text)
    log.info("重新分析任务已发送: resumeId=%d", resume_id)
