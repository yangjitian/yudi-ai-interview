"""
Voice Interview Opening Questions.

Centralized callable opening prompt module. The original YAML contents are kept
here so they can be used directly by voice interview flows.
"""

from __future__ import annotations

from typing import Any

from app.services.interview.prompt_engine import get_prompt_engine


SKILL_OPENING_QUESTIONS: dict[str, str] = {
    "java-backend": (
        "你好，我是本场面试官。第一个问题：请用1分钟介绍一个你深度参与的后端项目，"
        "按\"业务目标、核心链路、你的职责\"回答，说完我会立刻追问一个关键技术决策。"
    ),
    "java-backend-tencent": (
        "你好，我是本场面试官。第一个问题：请你结合一个真实项目，说明一次\"网络或并发\"问题的定位过程，"
        "包含现象、关键指标、根因和修复方案。"
    ),
    "bytedance-backend": (
        "你好，我是本场面试官。我们先做一道算法与数据结构热身题：请从哈希表、堆、栈、队列、树、图里选两个，"
        "结合一道你熟悉的题，口述你的建模思路、关键步骤、复杂度、边界与反例。"
    ),
    "ali-backend": (
        "你好，我是本场面试官。第一个问题：请用1分钟介绍一个你深度参与的后端项目，"
        "重点说业务目标、你主导的技术决策、一次线上故障与修复。"
    ),
    "python-backend": (
        "你好，我是本场面试官。第一个问题：请介绍一个你用Python深度参与的后端项目，"
        "按\"业务场景、技术选型理由、你负责的核心模块\"回答，说完我会追问一个设计决策。"
    ),
    "frontend": (
        "你好，我是本场面试官。第一个问题：请介绍一个你深度参与的前端项目，"
        "按\"产品目标、技术栈选型理由、你负责的核心页面或组件\"回答，说完我会追问一个性能或架构问题。"
    ),
    "system-design": (
        "你好，我是本场面试官。第一个问题：请用1分钟描述一个你参与过的系统，"
        "重点说明它的核心架构、最大的技术挑战、以及你是如何应对的，说完我会追问扩展性设计。"
    ),
    "algorithm": (
        "你好，我是本场面试官。第一个问题：请你口述一道算法题，"
        "不写代码，只讲\"问题建模、数据结构选型、步骤、复杂度、边界处理\"。"
    ),
    "test-development": (
        "你好，我是本场面试官。第一个问题：请你说一下你在测试开发方面的经历——"
        "比如参与过哪些测试项目，主要负责哪些测试类型（功能、自动化、性能、安全），用过什么工具或框架？"
    ),
    "ai-agent-dev": (
        "你好，我是本场面试官。第一个问题：请介绍一个你参与过的AI Agent或LLM应用项目，"
        "按\"业务场景、Agent架构设计、你负责的核心模块\"回答，说完我会追问一个工程落地的技术决策。"
    ),
}

ALGORITHM_SKILLS: list[str] = ["bytedance-backend", "algorithm"]

ALGORITHM_OPENING: str = (
    "你好，我是本场面试官。先做一道算法与数据结构热身题：请你从\"哈希表/堆/栈/队列/树/图\"里选两个，"
    "结合一道你熟悉的题，口述\"为什么选这个结构、核心步骤、时间复杂度、空间复杂度、边界条件与反例\"。"
    "本场不需要写代码，重点看你的思路和取舍。"
)

BACKEND_OPENING: str = (
    "你好，我是本场面试官。第一个问题：请用1分钟介绍一个你深度参与的项目，"
    "按三点回答：业务目标、你负责的核心模块、核心技术栈。说完我会立刻追问一个关键技术决策。"
)


def get_opening_question(skill_id: str) -> str:
  return SKILL_OPENING_QUESTIONS.get(skill_id, BACKEND_OPENING)


def is_algorithm_skill(skill_id: str) -> bool:
  return skill_id in ALGORITHM_SKILLS


def get_algorithm_opening() -> str:
  return ALGORITHM_OPENING


def get_backend_opening() -> str:
  return BACKEND_OPENING


def get_phase_opening(skill_id: str, phase: str) -> str:
  if phase.upper() == "INTRO":
    return get_opening_question(skill_id)
  if phase.upper() == "TECH" and is_algorithm_skill(skill_id):
    return get_algorithm_opening()
  return get_backend_opening()
