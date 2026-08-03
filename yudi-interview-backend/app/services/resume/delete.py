import logging

from app.infrastructure.storage.file_storage import delete_file
from app.repositories.interview_repository import InterviewRepository
from app.repositories.resume_repository import ResumeAnalysisRepository, ResumeRepository


log = logging.getLogger(__name__)


class ResumeDeleteService:
  def __init__(
      self,
      resume_repo: ResumeRepository,
      analysis_repo: ResumeAnalysisRepository,
      interview_repo: InterviewRepository,
  ):
    self.resume_repo = resume_repo
    self.analysis_repo = analysis_repo
    self.interview_repo = interview_repo

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

    await self.interview_repo.delete_by_resume_id(resume_id)
    await self.analysis_repo.delete_by_resume_id(resume_id)
    await self.resume_repo.delete(resume_id)
    log.info("Resume deleted: id=%d", resume_id)
