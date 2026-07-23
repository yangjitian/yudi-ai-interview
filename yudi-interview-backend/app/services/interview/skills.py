"""
Interview Skill Service - now backed by SkillManager with resources/ directory.
"""
import logging
from typing import Any

from app.services.interview.skill_manager import (
    SkillCategory,
    SkillDefinition,
    SkillDisplay,
    get_skill_manager,
)


log = logging.getLogger(__name__)


class InterviewSkillService:
    """Backed by SkillManager which loads from resources/skills/ directory."""

    def list_skills(self) -> list[dict[str, Any]]:
        mgr = get_skill_manager()
        skills = mgr.list_skills()
        return [s.to_dict() for s in skills]

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        mgr = get_skill_manager()
        skill = mgr.get_skill(skill_id)
        return skill.to_dict() if skill else None

    def get_skill_reference(self, skill_id: str) -> dict[str, str]:
        """Get reference content for a skill (used by frontend)."""
        mgr = get_skill_manager()
        return mgr.get_reference_content(skill_id)
