import logging
import re
from datetime import datetime

from app.core.errors import BusinessException, ErrorCode
from app.infrastructure.ai.prompt_security import sanitize
from app.infrastructure.ai.provider_registry import get_plain_chat_client
from app.infrastructure.ai.structured_output import StructuredOutputInvoker
from app.models.schedule import InterviewScheduleEntity, InterviewStatus
from app.models.schedule_dto import (
    CreateScheduleRequest,
    ParseResponse,
    ScheduleDTO,
    UpdateScheduleRequest,
)
from app.repositories.schedule_repository import ScheduleRepository
from app.utils.timezone_utils import get_beijing_now_naive, to_beijing_naive

log = logging.getLogger(__name__)

_DATE_TIME_PATTERN = re.compile(r"(\d{4}[-/]\d{2}[-/]\d{2})\s*[T ]\s*(\d{1,2}:\d{2})")
_COMPANY_PATTERN = re.compile(r"(?:公司|单位|组织)[：:]\s*([^\s\n]{1,50})")
_POSITION_PATTERN = re.compile(r"(?:岗位|职位|职务)[：:]\s*([^\s\n]{1,50})")
_ROUND_PATTERN = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*[轮场]")
_URL_PATTERN = re.compile(r"https?://[^\s\n]+")
_MEETING_ID_PATTERN = re.compile(r"(?:会议号|ID)[：:]?\s*(\d{9,})")
_PASSWORD_PATTERN = re.compile(r"密码[：:]?\s*(\d{4,})")
_CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


class InterviewScheduleService:
  def __init__(self, repo: ScheduleRepository):
    self.repo = repo

  async def create(self, req: CreateScheduleRequest) -> ScheduleDTO:
    entity = InterviewScheduleEntity(
        company_name=req.companyName,
        position=req.position,
        interview_time=to_beijing_naive(req.interviewTime),
        interview_type=req.interviewType,
        meeting_link=req.meetingLink,
        round_number=req.roundNumber,
        interviewer=req.interviewer,
        notes=req.notes,
        status=InterviewStatus.PENDING.value,
    )
    return self._to_dto(await self.repo.save(entity))

  async def get_by_id(self, schedule_id: int) -> ScheduleDTO:
    return self._to_dto(await self._get_by_id_or_throw(schedule_id))

  async def get_all(
      self,
      status: InterviewStatus | None,
      start: datetime | None,
      end: datetime | None,
  ) -> list[ScheduleDTO]:
    entities = await self.repo.list_all(
        status=status,
        start=to_beijing_naive(start) if start else None,
        end=to_beijing_naive(end) if end else None,
    )
    return [self._to_dto(entity) for entity in entities]

  async def update(
      self, schedule_id: int, req: CreateScheduleRequest | UpdateScheduleRequest
  ) -> ScheduleDTO:
    entity = await self._get_by_id_or_throw(schedule_id)
    values = req.model_dump(exclude_unset=True)
    field_mapping = {
        "companyName": "company_name",
        "interviewTime": "interview_time",
        "interviewType": "interview_type",
        "meetingLink": "meeting_link",
        "roundNumber": "round_number",
    }
    for field, value in values.items():
      if field == "interviewTime" and value is not None:
        value = to_beijing_naive(value)
      setattr(entity, field_mapping.get(field, field), value)
    return self._to_dto(await self.repo.update(entity))

  async def delete(self, schedule_id: int) -> None:
    if not await self.repo.delete(schedule_id):
      raise BusinessException(ErrorCode.INTERVIEW_SCHEDULE_NOT_FOUND)

  async def update_status(
      self, schedule_id: int, status: InterviewStatus
  ) -> ScheduleDTO:
    entity = await self._get_by_id_or_throw(schedule_id)
    entity.status = status.value
    return self._to_dto(await self.repo.update(entity))

  async def update_expired(self, cutoff: datetime | None = None) -> int:
    return await self.repo.update_expired(
        to_beijing_naive(cutoff) if cutoff else get_beijing_now_naive()
    )

  async def _get_by_id_or_throw(self, schedule_id: int) -> InterviewScheduleEntity:
    entity = await self.repo.find_by_id(schedule_id)
    if entity is None:
      raise BusinessException(ErrorCode.INTERVIEW_SCHEDULE_NOT_FOUND)
    return entity

  @staticmethod
  def _to_dto(entity: InterviewScheduleEntity) -> ScheduleDTO:
    return ScheduleDTO(
        id=entity.id,
        companyName=entity.company_name,
        position=entity.position,
        interviewTime=entity.interview_time,
        interviewType=entity.interview_type,
        meetingLink=entity.meeting_link,
        roundNumber=entity.round_number,
        interviewer=entity.interviewer,
        notes=entity.notes,
        status=InterviewStatus(entity.status),
        createdAt=entity.created_at,
        updatedAt=entity.updated_at,
    )


class InterviewParseService:
  def __init__(self):
    self._structured_invoker = StructuredOutputInvoker()

  async def parse(self, raw_text: str, source: str | None) -> ParseResponse:
    rule_result = self._parse_by_rule(raw_text, source)
    if rule_result is not None:
      return ParseResponse(
          success=True,
          data=rule_result,
          confidence=0.95,
          parseMethod="rule",
          log="规则解析成功",
      )

    try:
      result = await self._parse_with_ai(raw_text)
      return ParseResponse(
          success=True,
          data=result,
          confidence=0.8,
          parseMethod="ai",
          log="AI 解析成功",
      )
    except Exception as exc:
      log.warning("面试邀请解析失败: %s", exc)
      return ParseResponse(
          success=False,
          confidence=0.0,
          parseMethod="none",
          log="解析失败",
      )

  def _parse_by_rule(
      self, raw_text: str, source: str | None
  ) -> CreateScheduleRequest | None:
    company_match = _COMPANY_PATTERN.search(raw_text)
    position_match = _POSITION_PATTERN.search(raw_text)
    time_match = _DATE_TIME_PATTERN.search(raw_text)
    if company_match is None or position_match is None or time_match is None:
      return None

    interview_time = datetime.strptime(
        f"{time_match.group(1).replace('/', '-')} {time_match.group(2)}",
        "%Y-%m-%d %H:%M",
    )
    # public.sql 使用无时区时间戳，这里保留东八区本地时间语义。
    url_match = _URL_PATTERN.search(raw_text)
    round_match = _ROUND_PATTERN.search(raw_text)
    round_number = self._parse_round(round_match.group(1)) if round_match else 1

    meeting_link = url_match.group(0).rstrip("，。,.；;") if url_match else None
    if source == "tencent" and meeting_link is None:
      meeting_id = _MEETING_ID_PATTERN.search(raw_text)
      password = _PASSWORD_PATTERN.search(raw_text)
      if meeting_id:
        meeting_link = f"腾讯会议号: {meeting_id.group(1)}"
        if password:
          meeting_link += f" 密码: {password.group(1)}"

    interview_type = "ONSITE"
    if source in {"feishu", "tencent", "zoom"} or meeting_link:
      interview_type = "VIDEO"
    elif "电话" in raw_text:
      interview_type = "PHONE"

    return CreateScheduleRequest(
        companyName=company_match.group(1),
        position=position_match.group(1),
        interviewTime=interview_time,
        interviewType=interview_type,
        meetingLink=meeting_link,
        roundNumber=round_number,
    )

  async def _parse_with_ai(self, raw_text: str) -> CreateScheduleRequest:
    chat_client = await get_plain_chat_client(None)
    result = await self._structured_invoker.invoke(
        chat_model=chat_client,
        system_prompt=(
            "你是面试邀请信息提取助手。提取公司、岗位、面试时间、形式、"
            "会议链接、轮次、面试官和备注。面试时间使用 ISO 8601；"
            "形式只能是 ONSITE、VIDEO 或 PHONE。"
        ),
        user_prompt=f"待解析文本：\n{sanitize(raw_text)}",
        output_schema=CreateScheduleRequest,
        error_code=ErrorCode.AI_SERVICE_ERROR,
        error_prefix="面试邀请解析失败：",
        operation_name="面试邀请解析",
    )
    return CreateScheduleRequest.model_validate(result)

  @staticmethod
  def _parse_round(value: str) -> int:
    if value.isdigit():
      return int(value)
    return _CHINESE_NUMBERS.get(value, 1)
