"""
Prompt Engine - loads and renders prompt templates from resources directory.

Supports variable substitution using Python string formatting syntax.
Templates are .st files (simple template format) stored in resources/prompts/.
"""
import logging
import re
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)

RESOURCES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "resources"
PROMPTS_DIR = RESOURCES_DIR / "prompts"


def _camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case for template variable matching."""
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


class PromptEngine:
    """
    Loads prompt template files and renders them with variable substitution.

    Usage:
        engine = PromptEngine()
        rendered = engine.render("interview-question-skill-user", {
            "question_count": 5,
            "skill_name": "Java后端",
            ...
        })
    """

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def load_template(self, name: str) -> str:
        """
        Load a prompt template by name (without .st extension).
        Caches after first load.
        """
        if name in self._cache:
            log.debug("[CACHE HIT] prompt template | name=%s", name)
            return self._cache[name]

        if not PROMPTS_DIR.exists():
            log.warning("Prompts directory not found: %s", PROMPTS_DIR)
            return ""

        template_path = PROMPTS_DIR / f"{name}.st"
        if not template_path.exists():
            log.warning("Prompt template not found: %s", template_path)
            return ""

        try:
            content = template_path.read_text(encoding="utf-8")
            self._cache[name] = content
            return content
        except Exception as e:
            log.error("Failed to load prompt %s: %s", template_path, e)
            return ""

    def render(self, template_name: str, variables: dict[str, Any]) -> str:
        """
        Render a prompt template with the given variables.

        Variable names in templates use snake_case (e.g. {question_count},
        {skill_name}, {difficulty_description}).
        """
        template = self.load_template(template_name)
        if not template:
            return ""

        # Build a safe format mapping: try both the key as-is and
        # the snake_case version for CamelCase callers
        format_vars: dict[str, Any] = {}
        for key, value in variables.items():
            format_vars[key] = value
            snake = _camel_to_snake(key)
            if snake != key:
                format_vars[snake] = value

        try:
            return template.format(**format_vars)
        except KeyError as e:
            log.warning("Missing template variable %s in %s", e, template_name)
            # Try partial render to show what's available
            return template

    def render_system(self, template_name: str, variables: dict[str, Any]) -> str:
        """Alias for render, for semantic clarity."""
        return self.render(template_name, variables)

    def render_user(self, template_name: str, variables: dict[str, Any]) -> str:
        """Alias for render, for semantic clarity."""
        return self.render(template_name, variables)

    def build_interview_prompt(
        self,
        template_name: str,
        skill_name: str,
        skill_description: str,
        difficulty_description: str,
        question_count: int,
        follow_up_count: int,
        historical_section: str = "",
        reference_section: str = "",
        jd_text: str = "",
        resume_text: str = "",
        allocation_table: str = "",
    ) -> tuple[str, str]:
        """
        Build both system and user prompts for interview question generation.

        Returns (system_prompt, user_prompt).
        """
        system_vars = {
            "skill_name": skill_name,
            "skill_description": skill_description,
            "difficulty_description": difficulty_description,
            "question_count": question_count,
            "follow_up_count": follow_up_count,
            "historical_section": historical_section,
            "reference_section": reference_section,
            "jd_text": jd_text,
            "allocation_table": allocation_table,
            "resume_text": resume_text,
        }
        user_vars = {
            "skill_name": skill_name,
            "skill_description": skill_description,
            "difficulty_description": difficulty_description,
            "question_count": question_count,
            "follow_up_count": follow_up_count,
            "historical_section": historical_section,
            "reference_section": reference_section,
            "jd_text": jd_text,
            "resume_text": resume_text,
            "allocation_table": allocation_table,
        }

        # Templates are named: interview-question-{type}-{role}.st
        # System template: interview-question-{type}-system.st
        # User template: interview-question-{type}-user.st
        system_template = template_name.replace("-user.st", "-system.st")
        user_template = template_name

        system_prompt = self.render(system_template, system_vars)
        user_prompt = self.render(user_template, user_vars)

        return system_prompt, user_prompt

    def clear_cache(self) -> None:
        """Clear the template cache (useful for testing/reloading)."""
        self._cache.clear()


_prompt_engine: PromptEngine | None = None


def get_prompt_engine() -> PromptEngine:
    global _prompt_engine
    if _prompt_engine is None:
        _prompt_engine = PromptEngine()
        log.info("[INIT] PromptEngine singleton created")
    return _prompt_engine
