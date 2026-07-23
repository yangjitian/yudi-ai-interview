import logging

from fastapi import APIRouter, Response

from app.core.result import ApiResponse
from app.services.interview.skills import InterviewSkillService


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/skills", tags=["技能管理"])


@router.get("")
async def list_skills() -> ApiResponse:
    svc = InterviewSkillService()
    skills = svc.list_skills()
    return ApiResponse.success(data=skills)


@router.get("/{skill_id}")
async def get_skill(skill_id: str) -> ApiResponse:
    svc = InterviewSkillService()
    skill = svc.get_skill(skill_id)
    if skill is None:
        from app.core.errors import BusinessException, ErrorCode
        raise BusinessException(ErrorCode.NOT_FOUND, f"技能 '{skill_id}' 不存在")
    return ApiResponse.success(data=skill)


@router.get("/{skill_id}/reference")
async def get_skill_reference(skill_id: str) -> ApiResponse:
    """Get reference content for a skill (knowledge base for frontend)."""
    svc = InterviewSkillService()
    reference = svc.get_skill_reference(skill_id)
    return ApiResponse.success(data=reference)


@router.get("/{skill_id}/categories")
async def get_skill_categories(skill_id: str) -> ApiResponse:
    """Get categories and priorities for a skill."""
    svc = InterviewSkillService()
    skill = svc.get_skill(skill_id)
    if skill is None:
        from app.core.errors import BusinessException, ErrorCode
        raise BusinessException(ErrorCode.NOT_FOUND, f"技能 '{skill_id}' 不存在")
    return ApiResponse.success(data=skill.get("categories", []))
