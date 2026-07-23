from datetime import datetime

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.result import ApiResponse
from app.models.schedule import InterviewStatus
from app.models.schedule_dto import (
    CreateScheduleRequest,
    ParseRequest,
    ParseResponse,
    ScheduleDTO,
)
from app.repositories.schedule_repository import ScheduleRepository
from app.services.schedule.service import InterviewParseService, InterviewScheduleService


router = APIRouter(prefix="/api/interview-schedule", tags=["面试日程"])


@router.post("/parse", response_model=ApiResponse[ParseResponse])
async def parse_interview(req: ParseRequest) -> ApiResponse:
  result = await InterviewParseService().parse(req.rawText, req.source)
  return ApiResponse.success(data=result)


@router.post("", response_model=ApiResponse[ScheduleDTO])
async def create_schedule(
    req: CreateScheduleRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await InterviewScheduleService(ScheduleRepository(db)).create(req)
  return ApiResponse.success(data=result)


@router.get("", response_model=ApiResponse[list[ScheduleDTO]])
async def list_schedules(
    status: InterviewStatus | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await InterviewScheduleService(ScheduleRepository(db)).get_all(
      status, start, end
  )
  return ApiResponse.success(data=result)


@router.get("/{schedule_id}", response_model=ApiResponse[ScheduleDTO])
async def get_schedule(
    schedule_id: int = Path(description="日程 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await InterviewScheduleService(ScheduleRepository(db)).get_by_id(schedule_id)
  return ApiResponse.success(data=result)


@router.put("/{schedule_id}", response_model=ApiResponse[ScheduleDTO])
async def update_schedule(
    req: CreateScheduleRequest,
    schedule_id: int = Path(description="日程 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await InterviewScheduleService(ScheduleRepository(db)).update(
      schedule_id, req
  )
  return ApiResponse.success(data=result)


@router.delete("/{schedule_id}", response_model=ApiResponse[None])
async def delete_schedule(
    schedule_id: int = Path(description="日程 ID"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  await InterviewScheduleService(ScheduleRepository(db)).delete(schedule_id)
  return ApiResponse.success()


@router.patch("/{schedule_id}/status", response_model=ApiResponse[ScheduleDTO])
@router.put("/{schedule_id}/status", response_model=ApiResponse[ScheduleDTO])
async def update_schedule_status(
    schedule_id: int = Path(description="日程 ID"),
    status: InterviewStatus = Query(),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
  result = await InterviewScheduleService(ScheduleRepository(db)).update_status(
      schedule_id, status
  )
  return ApiResponse.success(data=result)
