"""
Skill System - loads and manages skill definitions from resources directory.

Manages:
- Skill definitions (skill.meta.yml equivalent)
- Skill categories and reference content
- Prompt templates per skill
"""
import logging
import threading
import time
from pathlib import Path
from typing import Any

import yaml


log = logging.getLogger(__name__)

RESOURCES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "resources"
SKILLS_DIR = RESOURCES_DIR / "skills"
PROMPTS_DIR = RESOURCES_DIR / "prompts"
SHARED_REFS_DIR = SKILLS_DIR / "_shared" / "references"


class SkillCategory:
    def __init__(
        self,
        key: str,
        label: str,
        priority: str,
        ref: str | None = None,
        shared: bool | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.priority = priority  # CORE | NORMAL | ALWAYS_ONE
        self.ref = ref
        self.shared = shared

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "priority": self.priority,
            "ref": self.ref,
            "shared": self.shared,
        }


class SkillDisplay:
    def __init__(
        self,
        icon: str,
        gradient: str,
        icon_bg: str,
        icon_color: str,
    ) -> None:
        self.icon = icon
        self.gradient = gradient
        self.icon_bg = icon_bg
        self.icon_color = icon_color

    def to_dict(self) -> dict[str, str]:
        return {
            "icon": self.icon,
            "gradient": self.gradient,
            "iconBg": self.icon_bg,
            "iconColor": self.icon_color,
        }


class SkillDefinition:
    def __init__(
        self,
        skill_id: str,
        name: str,
        description: str,
        categories: list[SkillCategory],
        display: SkillDisplay | None = None,
        system_prompt: str = "",
        user_prompt_template: str = "",
    ) -> None:
        self.id = skill_id
        self.name = name
        self.description = description
        self.categories = categories
        self.display = display
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "categories": [c.to_dict() for c in self.categories],
            "display": self.display.to_dict() if self.display else {},
            "systemPrompt": self.system_prompt,
        }

    def get_categories_by_priority(self, priority: str) -> list[SkillCategory]:
        return [c for c in self.categories if c.priority == priority]

    def get_core_categories(self) -> list[SkillCategory]:
        return self.get_categories_by_priority("CORE")

    def get_reference_files(self) -> list[str]:
        refs = []
        for cat in self.categories:
            if cat.ref:
                refs.append(cat.ref)
        return refs


def _load_yaml(path: Path) -> dict[str, Any] | None:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to load YAML %s: %s", path, e)
        return None


def _load_reference(ref_name: str | None) -> str:
    if not ref_name:
        return ""
    ref_path = SHARED_REFS_DIR / ref_name
    if ref_path.exists():
        return ref_path.read_text(encoding="utf-8")
    # Try in skill-specific directory
    return ""


def _build_system_prompt(skill_dir: Path, definition: SkillDefinition) -> str:
    """Build system prompt from SKILL.md file."""
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        return skill_md.read_text(encoding="utf-8")
    return f"# {definition.name}\n\n{definition.description}"


class SkillManager:
    """
    Singleton manager that loads all skill definitions from the resources directory.
    Provides lookup by skill_id and caching.
    """

    _instance: "SkillManager | None" = None

    def __new__(cls) -> "SkillManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._skills: dict[str, SkillDefinition] = {}
        self._reference_cache: dict[str, dict[str, str]] = {}
        self._reference_file_cache: dict[str, str] = {}
        self._loaded = False
        self._cache_lock = threading.RLock()
        self._initialized = True

    def load(self) -> None:
        if self._loaded:
            return
        with self._cache_lock:
            if self._loaded:
                return
            started_at = time.perf_counter()
            log.info("[CACHE MISS] skill registry loading from disk")
            self._skills.clear()
            if not SKILLS_DIR.exists():
                log.warning("Skills directory not found: %s", SKILLS_DIR)
                self._loaded = True
                return

            for skill_dir in SKILLS_DIR.iterdir():
                if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
                    continue
                self._load_skill(skill_dir)

            self._loaded = True
            log.info(
                "[CACHE] skill registry loaded and cached | skills=%d elapsed=%.3fs",
                len(self._skills),
                time.perf_counter() - started_at,
            )

    def _load_skill(self, skill_dir: Path) -> None:
        meta_path = skill_dir / "skill.meta.yml"
        if not meta_path.exists():
            log.warning("No skill.meta.yml in %s", skill_dir)
            return

        meta = _load_yaml(meta_path)
        if not meta:
            return

        skill_id = skill_dir.name
        display_name = meta.get("displayName", skill_id)
        display_data = meta.get("display", {})
        display = SkillDisplay(
            icon=display_data.get("icon", "📋"),
            gradient=display_data.get("gradient", ""),
            icon_bg=display_data.get("iconBg", ""),
            icon_color=display_data.get("iconColor", ""),
        )

        categories = []
        for cat_data in meta.get("categories", []):
            categories.append(
                SkillCategory(
                    key=cat_data.get("key", ""),
                    label=cat_data.get("label", ""),
                    priority=cat_data.get("priority", "NORMAL"),
                    ref=cat_data.get("ref"),
                    shared=cat_data.get("shared"),
                )
            )

        definition = SkillDefinition(
            skill_id=skill_id,
            name=display_name,
            description=meta.get("description", ""),
            categories=categories,
            display=display,
            system_prompt=_build_system_prompt(skill_dir, definition=None),  # type: ignore[arg-type]
        )
        # Build prompt after definition exists
        definition.system_prompt = _build_system_prompt(skill_dir, definition)

        self._skills[skill_id] = definition

    def list_skills(self) -> list[SkillDefinition]:
        self.load()
        return list(self._skills.values())

    def get_skill(self, skill_id: str) -> SkillDefinition | None:
        self.load()
        skill = self._skills.get(skill_id)
        if skill:
            log.debug("[CACHE HIT] skill meta | skill_id=%s", skill_id)
        return skill

    def get_reference_content(self, skill_id: str) -> dict[str, str]:
        """对照 Java computeIfAbsent：同一 skill 的 reference 只读取一次磁盘。"""
        started_at = time.perf_counter()
        with self._cache_lock:
            cached = self._reference_cache.get(skill_id)
            if cached is not None:
                log.debug("[CACHE HIT] reference | skill_id=%s", skill_id)
                log.info(
                    "[PERF] SkillManager.get_reference_content: %.3fs | skill_id=%s files=%d cached=yes",
                    time.perf_counter() - started_at,
                    skill_id,
                    len(cached),
                )
                return cached

            log.info("[CACHE MISS] reference loading from disk | skill_id=%s", skill_id)
            result = self._load_reference_content_from_disk(skill_id)
            self._reference_cache[skill_id] = result
            elapsed = time.perf_counter() - started_at
            log.info(
                "[CACHE] reference loaded and cached | skill_id=%s files=%d elapsed=%.3fs",
                skill_id,
                len(result),
                elapsed,
            )
            log.info(
                "[PERF] SkillManager.get_reference_content: %.3fs | skill_id=%s files=%d cached=no",
                elapsed,
                skill_id,
                len(result),
            )
            return result

    def _load_reference_content_from_disk(self, skill_id: str) -> dict[str, str]:
        skill = self.get_skill(skill_id)
        if not skill:
            return {}
        result: dict[str, str] = {}
        for ref_name in skill.get_reference_files():
            content = self._load_reference_cached(ref_name)
            if content:
                result[ref_name] = content
        return result

    def _load_reference_cached(self, ref_name: str) -> str:
        cached = self._reference_file_cache.get(ref_name)
        if cached is not None:
            log.debug("[CACHE HIT] reference file | ref=%s", ref_name)
            return cached
        content = _load_reference(ref_name)
        self._reference_file_cache[ref_name] = content
        return content

    def get_reference_content_for_categories(
        self, categories: list[SkillCategory]
    ) -> dict[str, str]:
        """Get reference content for a list of categories."""
        started_at = time.perf_counter()
        result: dict[str, str] = {}
        seen: set[str] = set()
        with self._cache_lock:
            for cat in categories:
                if cat.ref and cat.ref not in seen:
                    content = self._load_reference_cached(cat.ref)
                    if content:
                        result[cat.ref] = content
                        seen.add(cat.ref)
        log.info(
            "[PERF] SkillManager.get_reference_content_for_categories: %.3fs | files=%d",
            time.perf_counter() - started_at,
            len(result),
        )
        return result

    def reload(self) -> None:
        """Force reload from disk."""
        with self._cache_lock:
            self._skills.clear()
            self._reference_cache.clear()
            self._reference_file_cache.clear()
            self._loaded = False
        self.load()


_skill_manager: SkillManager | None = None


def get_skill_manager() -> SkillManager:
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager()
        log.info("[INIT] SkillManager singleton created")
    return _skill_manager
