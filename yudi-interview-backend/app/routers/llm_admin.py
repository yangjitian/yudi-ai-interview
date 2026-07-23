import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.result import ApiResponse
from app.models.llm_provider_dto import (
    AsrConfigDTO,
    CreateProviderRequest,
    GlobalSettingDTO,
    ProviderDTO,
    TestProviderRequest,
    TestProviderResponse,
    TtsConfigDTO,
    UpdateAsrConfigRequest,
    UpdateGlobalSettingRequest,
    UpdateProviderRequest,
    UpdateTtsConfigRequest,
    VoiceConfigTestResultDTO,
)
from app.services.llm.admin import LlmProviderAdminService


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/llm", tags=["LLM 管理"])


@router.get("/providers", response_model=ApiResponse[list[ProviderDTO]])
async def list_providers(db: AsyncSession = Depends(get_db)) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.list_providers()
    return ApiResponse.success(data=result)


@router.get("/providers/{provider_id}", response_model=ApiResponse[ProviderDTO])
async def get_provider(provider_id: str, db: AsyncSession = Depends(get_db)) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.get_provider(provider_id)
    return ApiResponse.success(data=result)


@router.post("/providers", response_model=ApiResponse[dict])
async def create_provider(
    req: CreateProviderRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.create_provider(req.model_dump())
    return ApiResponse.success(data=result)


@router.put("/providers/{provider_id}", response_model=ApiResponse[dict])
async def update_provider(
    req: UpdateProviderRequest,
    provider_id: str,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.update_provider(provider_id, req.model_dump(exclude_none=True))
    return ApiResponse.success(data=result)


@router.delete("/providers/{provider_id}", response_model=ApiResponse[None])
async def delete_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    await svc.delete_provider(provider_id)
    return ApiResponse.success()


@router.post("/providers/test", response_model=ApiResponse[TestProviderResponse])
async def test_provider(
    req: TestProviderRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.test_provider(req.provider_id)
    return ApiResponse.success(data=result)


@router.get("/settings", response_model=ApiResponse[GlobalSettingDTO])
async def get_global_setting(db: AsyncSession = Depends(get_db)) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.get_global_setting()
    return ApiResponse.success(data=result)


@router.put("/settings", response_model=ApiResponse[GlobalSettingDTO])
async def update_global_setting(
    req: UpdateGlobalSettingRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.update_global_setting(req.model_dump(exclude_none=True))
    return ApiResponse.success(data=result)


@router.post("/reload", response_model=ApiResponse[dict])
async def reload_providers() -> ApiResponse:
    from app.infrastructure.ai.provider_registry import reload
    await reload()
    return ApiResponse.success(data={"status": "reloaded"})


@router.get("/voice/asr", response_model=ApiResponse[AsrConfigDTO])
async def get_asr_config(db: AsyncSession = Depends(get_db)) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.get_asr_config()
    return ApiResponse.success(data=result)


@router.put("/voice/asr", response_model=ApiResponse[AsrConfigDTO])
async def update_asr_config(
    req: UpdateAsrConfigRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.update_asr_config(req.model_dump(exclude_none=True))
    return ApiResponse.success(data=result)


@router.get("/voice/tts", response_model=ApiResponse[TtsConfigDTO])
async def get_tts_config(db: AsyncSession = Depends(get_db)) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.get_tts_config()
    return ApiResponse.success(data=result)


@router.put("/voice/tts", response_model=ApiResponse[TtsConfigDTO])
async def update_tts_config(
    req: UpdateTtsConfigRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.update_tts_config(req.model_dump(exclude_none=True))
    return ApiResponse.success(data=result)


@router.post("/voice/asr/test", response_model=ApiResponse[VoiceConfigTestResultDTO])
async def test_asr_config(db: AsyncSession = Depends(get_db)) -> ApiResponse:
    svc = LlmProviderAdminService(db)
    result = await svc.test_asr_config()
    return ApiResponse.success(data=result)
