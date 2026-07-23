"""
Interview Question Generator - generates questions using skill system and prompt templates.

Fully refactored to use:
- SkillManager: loads skill definitions and categories from resources/
- PromptEngine: loads and renders .st prompt templates
- Reference content: injected into prompts from skills/_shared/references/
"""
import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Coroutine
from typing import Any

from pydantic import BaseModel, Field

from app.core.errors import ErrorCode
from app.config.settings import get_settings
from app.infrastructure.ai.provider_registry import get_plain_chat_client
from app.infrastructure.ai.structured_output import StructuredOutputInvoker
from app.services.interview.prompt_engine import get_prompt_engine
from app.services.interview.skill_manager import SkillCategory, SkillDefinition, get_skill_manager


log = logging.getLogger(__name__)

MAX_REFERENCE_CHARS_PER_FILE = 800
MAX_REFERENCE_FILES = 3
MAX_TOTAL_REFERENCE_CHARS = 2000


async def _monitor_event_loop_lag(stop_event: asyncio.Event) -> None:
    interval = float(os.getenv("LLM_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS", "0.5"))
    warning_ms = float(os.getenv("LLM_EVENT_LOOP_LAG_WARNING_MS", "200"))
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        expected = loop.time() + interval
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            lag_ms = max(0.0, (loop.time() - expected) * 1000)
            if lag_ms >= warning_ms:
                log.warning("[PERF] event_loop_lag | lag_ms=%.1f threshold_ms=%.1f", lag_ms, warning_ms)

# Difficulty descriptions (matches Java version)
DIFFICULTY_DESCRIPTIONS = {
    "junior": "初级：考察基础知识、概念理解、简单应用",
    "mid": "中级：考察原理理解、实际应用、问题解决能力",
    "senior": "高级：考察系统设计、架构思维、深度原理、性能优化",
}

GENERIC_FALLBACK_QUESTIONS = [
    ("请描述一个你主导解决的技术难题，你的分析思路是什么？", "GENERAL"),
    ("你在做技术方案选型时，通常考虑哪些因素？请举例说明。", "GENERAL"),
    ("请分享一次你处理线上故障的经历，从发现到修复的完整过程。", "GENERAL"),
    ("你如何保证代码质量？介绍你实践过的有效手段。", "GENERAL"),
    ("描述一个你做过的技术优化案例，优化的动机、方案和效果。", "GENERAL"),
    ("你在团队协作中遇到过最大的分歧是什么？如何解决的？", "GENERAL"),
]


class GeneratedQuestion(BaseModel):
    question: str
    type: str
    followUps: list[str] = Field(default_factory=list)
    topicSummary: str = ""


class GeneratedQuestionList(BaseModel):
    questions: list[GeneratedQuestion] = Field(default_factory=list)


def _parse_questions_json(text: str) -> list[dict]:
    """Parse JSON questions from LLM response."""
    text = text.strip()
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    try:
        return json.loads(text)
    except Exception:
        return []


def _build_reference_section(references: dict[str, str]) -> str:
    """Build a reference section from reference content dict."""
    if not references:
        return ""
    sections: list[str] = []
    total_chars = 0
    for index, (filename, content) in enumerate(references.items()):
        if index >= MAX_REFERENCE_FILES:
            log.info("[PERF] reference truncated: skipped %d files", len(references) - index)
            break

        remaining_chars = MAX_TOTAL_REFERENCE_CHARS - total_chars
        if remaining_chars <= 0:
            log.info("[PERF] reference total cap reached at file %d", index)
            break
        chunk = content[:min(MAX_REFERENCE_CHARS_PER_FILE, remaining_chars)]
        sections.append(f"### {filename}\n\n{chunk}")
        total_chars += len(chunk)
        if len(chunk) < min(len(content), MAX_REFERENCE_CHARS_PER_FILE):
            log.info("[PERF] reference total cap reached at file %d", index)
            break

    log.info(
        "[PERF] reference section built: files=%d total_chars=%d",
        len(sections),
        total_chars,
    )
    return "\n\n".join(sections)


def _build_allocation_table(
    categories: list[dict], question_count: int
) -> str:
    """Build the question distribution table for prompts."""
    if not categories:
        return ""
    rows = ["| 方向 | 数量 | 说明 |", "|------|------|------|"]
    for cat in categories:
        rows.append(f"| {cat.get('label', cat.get('key', ''))} | {cat.get('count', 1)} | {cat.get('note', '')} |")
    return "\n".join(rows)


def _build_category_table(categories: list[dict]) -> str:
    """Build a simple category table."""
    rows = ["| 方向 | 数量 | 说明 |", "|------|------|------|"]
    for cat in categories:
        rows.append(f"| {cat.get('label', cat.get('key', ''))} | {cat.get('count', 1)} | {cat.get('note', '')} |")
    return "\n".join(rows)


def _build_reference_file_list() -> str:
    """Build reference file list for JD parsing."""
    import os
    ref_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "resources", "skills", "_shared", "references"
    )
    if not os.path.exists(ref_dir):
        return ""
    files = sorted(f for f in os.listdir(ref_dir) if f.endswith(".md"))
    rows = ["| 文件名 | 范围 |", "|------|------|"]
    for f in files:
        scope = "shared"
        rows.append(f"| {f} | {scope} |")
    return "\n".join(rows)


class QuestionGenerator:
    """Generates interview questions using skill system + LLM."""

    def __init__(self) -> None:
        self._skill_mgr = get_skill_manager()
        self._prompt_engine = get_prompt_engine()
        self._structured_invoker = StructuredOutputInvoker()

    async def generate_questions_for_skill(
        self,
        skill_id: str,
        difficulty: str,
        question_count: int,
        resume_text: str | None = None,
        llm_provider: str | None = None,
        custom_categories: list[dict] | None = None,
        jd_text: str | None = None,
        historical_topics: list[str] | None = None,
        raise_on_error: bool = False,
    ) -> list[dict]:
        """
        Generate interview questions for a skill-based interview.

        Uses the skill's categories and reference content to build
        targeted prompts for the LLM.
        """
        total_started_at = time.perf_counter()
        skill_started_at = time.perf_counter()
        skill = self._skill_mgr.get_skill(skill_id)
        if skill_id == "custom" and custom_categories:
            categories = [
                SkillCategory(
                    key=str(cat.get("key") or "general"),
                    label=str(cat.get("label") or "通用技能"),
                    priority=str(cat.get("priority") or "NORMAL"),
                    ref=cat.get("ref"),
                    shared=cat.get("shared"),
                )
                for cat in custom_categories
            ]
            skill = SkillDefinition(
                skill_id="custom",
                name="自定义岗位",
                description="根据用户提供的职位描述生成针对性面试题",
                categories=categories,
            )
        log.info(
            "[PERF] QGen skill meta load: %.3fs | skill_id=%s found=%s",
            time.perf_counter() - skill_started_at,
            skill_id,
            skill is not None,
        )
        if not skill:
            log.warning("Skill not found: %s, using fallback", skill_id)
            result = self._fallback_questions(
                skill_id, difficulty, question_count, custom_categories=custom_categories,
                resume_text=resume_text, jd_text=jd_text,
            )
            log.info("[PERF] QGen total: %.3fs", time.perf_counter() - total_started_at)
            return result

        difficulty_desc = DIFFICULTY_DESCRIPTIONS.get(difficulty, DIFFICULTY_DESCRIPTIONS["mid"])

        # Get reference content for all categories
        reference_started_at = time.perf_counter()
        references = (
            self._skill_mgr.get_reference_content_for_categories(skill.categories)
            if skill_id == "custom"
            else self._skill_mgr.get_reference_content(skill_id)
        )
        log.info(
            "[PERF] QGen reference load: %.3fs | files=%d total_chars=%d",
            time.perf_counter() - reference_started_at,
            len(references),
            sum(len(content) for content in references.values()),
        )

        prompt_started_at = time.perf_counter()
        reference_section = _build_reference_section(references)

        # Build category distribution
        categories = self._build_category_distribution(
            skill, question_count, custom_categories
        )

        # Build historical topics section
        historical = self._build_historical_section(historical_topics or [])

        # Build allocation table
        allocation_table = _build_allocation_table(categories, question_count)

        # Get JD section
        jd_section = ""
        if jd_text:
            jd_section = f"\n## 职位描述\n{jd_text[:2000]}\n"

        # Build reference file list
        ref_file_list = _build_reference_file_list()

        # Build system + user prompts
        system_vars = {
            "skill_name": skill.name,
            "skill_description": skill.description,
            "difficulty_description": difficulty_desc,
            "question_count": question_count,
            "follow_up_count": 1,
            "allocation_table": allocation_table,
            "historical_section": historical,
            "reference_section": reference_section,
            "jd_section": jd_section,
            "reference_file_list": ref_file_list,
        }

        user_vars = {
            "skill_name": skill.name,
            "skill_description": skill.description,
            "difficulty_description": difficulty_desc,
            "question_count": question_count,
            "follow_up_count": 1,
            "allocation_table": allocation_table,
            "historical_section": historical,
            "reference_section": reference_section,
            "jd_text": jd_section,
            "resume_text": resume_text or "",
        }

        # Choose template based on whether we have resume text
        if resume_text:
            system_tmpl = "interview-question-resume-system"
            user_tmpl = "interview-question-resume-user"
        else:
            system_tmpl = "interview-question-skill-system"
            user_tmpl = "interview-question-skill-user"

        system_prompt = self._prompt_engine.render(system_tmpl, system_vars)
        user_prompt = self._prompt_engine.render(user_tmpl, user_vars)
        if resume_text and jd_section:
            user_prompt += jd_section
        log.info(
            "[PERF] QGen prompt render: %.3fs | system_len=%d user_len=%d",
            time.perf_counter() - prompt_started_at,
            len(system_prompt),
            len(user_prompt),
        )

        try:
            llm_started_at = time.perf_counter()
            response_len = 0
            try:
                chat = await get_plain_chat_client(llm_provider)
                response = await self._structured_invoker.invoke(
                    chat_model=chat,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    output_schema=GeneratedQuestionList,
                    error_code=ErrorCode.INTERVIEW_QUESTION_GENERATION_FAILED,
                    error_prefix="面试题生成失败：",
                    operation_name="面试题生成",
                )
                response_len = len(response.model_dump_json())
            finally:
                log.info(
                    "[PERF] QGen LLM call: %.3fs | response_len=%d",
                    time.perf_counter() - llm_started_at,
                    response_len,
                )

            parse_started_at = time.perf_counter()
            generated = response.questions[:question_count]
            questions = [
                {"question": item.question, "category": item.type}
                for item in generated
                if item.question.strip()
            ]
            if len(questions) < question_count:
                questions.extend(self._fallback_questions(
                    skill_id=skill_id,
                    difficulty=difficulty,
                    count=question_count - len(questions),
                    skill=skill,
                    custom_categories=custom_categories,
                    resume_text=resume_text,
                    jd_text=jd_text,
                ))
            result = questions[:question_count]
            log.info(
                "[PERF] QGen JSON parse: %.3fs | questions=%d",
                time.perf_counter() - parse_started_at,
                len(result),
            )
            log.info("[PERF] QGen total: %.3fs", time.perf_counter() - total_started_at)
            return result
        except Exception as e:
            log.error("Question generation failed: skill_id=%s error=%s", skill_id, e, exc_info=True)
            if raise_on_error:
                raise
            result = self._fallback_questions(
                skill_id, difficulty, question_count, skill=skill,
                custom_categories=custom_categories, resume_text=resume_text, jd_text=jd_text,
            )
            log.info("[PERF] QGen total: %.3fs", time.perf_counter() - total_started_at)
            return result

    def _build_category_distribution(
        self,
        skill: Any,
        total_count: int,
        custom: list[dict] | None,
    ) -> list[dict]:
        """Build category distribution for question generation."""
        source = custom or [cat.to_dict() for cat in skill.categories]
        if not source:
            return []

        ordered = sorted(
            source,
            key=lambda cat: {"ALWAYS_ONE": 0, "CORE": 1, "NORMAL": 2}.get(
                str(cat.get("priority") or "NORMAL"), 3
            ),
        )
        selected = ordered[:total_count]
        result = [
            {
                "key": str(cat.get("key") or "general"),
                "label": str(cat.get("label") or "通用技能"),
                "count": 1,
                "note": str(cat.get("priority") or "NORMAL"),
            }
            for cat in selected
        ]
        for index in range(total_count - len(result)):
            result[index % len(result)]["count"] += 1
        return result

    def _build_historical_section(self, topics: list[str]) -> str:
        """Build historical topics section for avoiding duplicates."""
        if not topics:
            return "（无）"
        return "、".join(topics[-10:])

    def _fallback_questions(
        self,
        skill_id: str,
        difficulty: str,
        count: int,
        skill: Any | None = None,
        custom_categories: list[dict] | None = None,
        resume_text: str | None = None,
        jd_text: str | None = None,
    ) -> list[dict]:
        """Fallback question generation when LLM fails."""
        categories = custom_categories or (
            [cat.to_dict() for cat in skill.categories] if skill else []
        )
        if not categories:
            return [
                {"question": question, "category": category}
                for question, category in GENERIC_FALLBACK_QUESTIONS[:count]
            ]

        difficulty_templates = {
            "junior": "请说明{label}的核心概念和常见使用方式。",
            "mid": "请结合实际项目说明{label}的工作原理、应用场景和常见问题。",
            "senior": "请围绕{label}设计一个复杂业务方案，并分析架构权衡、性能瓶颈和故障处理。",
        }
        template = difficulty_templates.get(difficulty, difficulty_templates["mid"])
        if resume_text:
            template = "结合你简历中的项目经历，" + template
        elif jd_text:
            template = "结合目标岗位要求，" + template
        return [
            {
                "question": template.format(
                    label=str(categories[i % len(categories)].get("label") or "通用技能")
                ),
                "category": str(
                    categories[i % len(categories)].get("key")
                    or categories[i % len(categories)].get("label")
                    or "general"
                ),
            }
            for i in range(count)
        ]


_generator: QuestionGenerator | None = None


def get_question_generator() -> QuestionGenerator:
    global _generator
    if _generator is None:
        _generator = QuestionGenerator()
        log.info("[INIT] QuestionGenerator singleton created")
    return _generator


async def generate_questions(
    skill_id: str,
    difficulty: str,
    question_count: int,
    resume_text: str | None = None,
    llm_provider: str | None = None,
    custom_categories: list[dict] | None = None,
    jd_text: str | None = None,
    historical_topics: list[str] | None = None,
) -> list[dict]:
    """Public API for question generation."""
    gen = get_question_generator()
    return await gen.generate_questions_for_skill(
        skill_id=skill_id,
        difficulty=difficulty,
        question_count=question_count,
        resume_text=resume_text,
        llm_provider=llm_provider,
        custom_categories=custom_categories,
        jd_text=jd_text,
        historical_topics=historical_topics,
    )


async def generate_questions_parallel(
    session_id: str,
    skill_id: str,
    difficulty: str,
    question_count: int,
    resume_text: str | None = None,
    llm_provider: str | None = None,
    custom_categories: list[dict] | None = None,
    jd_text: str | None = None,
) -> tuple[list[dict], bool, str | None]:
    """并行生成简历题和方向题，失败时按层级降级。"""
    generator = get_question_generator()
    parallel_started_at = time.perf_counter()
    timeout = get_settings().interview.question_generation_timeout_seconds
    coroutines = []
    labels = []
    if resume_text:
        resume_count = max(1, (question_count * 6 + 5) // 10)
        direction_count = question_count - resume_count
        labels.append("resume")
        coroutines.append(generator.generate_questions_for_skill(
            skill_id, difficulty, resume_count, resume_text=resume_text,
            llm_provider=llm_provider, custom_categories=custom_categories,
            jd_text=jd_text, raise_on_error=True,
        ))
        if direction_count > 0:
            labels.append("skill")
            coroutines.append(generator.generate_questions_for_skill(
                skill_id, difficulty, direction_count, resume_text=None,
                llm_provider=llm_provider, custom_categories=custom_categories,
                jd_text=jd_text, raise_on_error=True,
            ))
    else:
        labels.append("skill")
        coroutines.append(generator.generate_questions_for_skill(
            skill_id, difficulty, question_count, resume_text=None,
            llm_provider=llm_provider, custom_categories=custom_categories,
            jd_text=jd_text, raise_on_error=True,
        ))
    watchdog_stop = asyncio.Event()
    watchdog_task = asyncio.create_task(_monitor_event_loop_lag(watchdog_stop))
    log.info(
        "[LLM_HTTP] qgen_deadline | session_id=%s skill_id=%s branches=%s question_count=%d "
        "timeout=%.1fs source=APP_INTERVIEW_QUESTION_GENERATION_TIMEOUT_SECONDS",
        session_id, skill_id, ",".join(labels), question_count, float(timeout),
    )

    async def _run_branch(
        label: str,
        coroutine: Coroutine[Any, Any, list[dict]],
    ) -> list[dict]:
        branch_task = asyncio.create_task(coroutine)
        try:
            return await asyncio.wait_for(branch_task, timeout=timeout)
        except asyncio.TimeoutError as exc:
            if not branch_task.cancelled():
                raise
            log.error(
                "[LLM_HTTP] outer_timeout | operation=question_generation session_id=%s branch=%s "
                "timeout=%.1fs source=APP_INTERVIEW_QUESTION_GENERATION_TIMEOUT_SECONDS "
                "cancellation_source=asyncio.wait_for",
                session_id, label, float(timeout),
            )
            raise TimeoutError(
                f"outer deadline {timeout}s exceeded "
                "(APP_INTERVIEW_QUESTION_GENERATION_TIMEOUT_SECONDS)"
            ) from exc

    try:
        results = await asyncio.gather(
            *(_run_branch(label, coro) for label, coro in zip(labels, coroutines)),
            return_exceptions=True,
        )
    finally:
        watchdog_stop.set()
        await watchdog_task
    successful: list[dict] = []
    failures: list[str] = []
    for label, result in zip(labels, results):
        if isinstance(result, Exception):
            detail = str(result).strip() or "deadline exceeded"
            reason = f"{label}: {type(result).__name__}: {detail}"
            failures.append(reason)
            log.warning(
                "[PERF] QGen branch failed | session_id=%s skill_id=%s reason=%s elapsed=%.3fs",
                session_id, skill_id, reason, time.perf_counter() - parallel_started_at,
            )
            log.warning("题目生成分路失败: %s", reason)
        else:
            successful.extend(result)
    unique: list[dict] = []
    seen: set[str] = set()
    for question in successful:
        text = str(question.get("question") or "").strip()
        if text and text not in seen:
            seen.add(text)
            unique.append(question)
    if len(unique) >= question_count:
        return unique[:question_count], bool(failures), "; ".join(failures) if failures else None

    missing_count = question_count - len(unique)
    fallback = generator._fallback_questions(
        skill_id, difficulty, missing_count,
        skill=generator._skill_mgr.get_skill(skill_id),
        custom_categories=custom_categories, resume_text=resume_text, jd_text=jd_text,
    )
    reason = "; ".join(failures) or "LLM 返回题目数量不足"
    log.warning(
        "题目生成部分降级为模板题: session_id=%s skill_id=%s generated=%d fallback=%d reason=%s",
        session_id, skill_id, len(unique), len(fallback), reason,
    )
    log.warning(
        "[PERF] QGen fallback_template | session_id=%s skill_id=%s reason=%s elapsed=%.3fs",
        session_id, skill_id, reason, time.perf_counter() - parallel_started_at,
    )
    return (unique + fallback)[:question_count], True, reason
