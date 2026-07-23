import logging

from app.infrastructure.storage.file_storage import delete_file
from app.repositories.resume_repository import ResumeRepository


log = logging.getLogger(__name__)


class ResumeDeleteService:
  def __init__(self, resume_repo: ResumeRepository):
    self.resume_repo = resume_repo

  async def delete_resume(self, resume_id: int) -> None:
    entity = await self.resume_repo.find_by_id(resume_id)
    if entity is None:
      return

    storage_key = entity.storage_key
    if storage_key:
      try:
        await delete_file(storage_key)
      except Exception as e:
        log.warning("S3 delete failed for resume %d: %s", resume_id, e)

    await self.resume_repo.delete(resume_id)
    log.info("Resume deleted: id=%d", resume_id)
