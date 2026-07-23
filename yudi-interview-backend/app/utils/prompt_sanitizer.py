"""
Prompt 注入净化工具。

参考 Java PromptSanitizer.java 实现，过滤以下风险：
1. 角色扮演注入（行首 system/user/assistant 等）
2. 指令覆盖攻击（ignore previous instructions 等）
3. 分隔符伪造（---简历内容--- 等）
4. 边界标签伪造（<data-boundary> 等）

仅用于裸拼接点（无模板包裹），有模板的插值点由 system prompt 保护。
"""

import re
import uuid
import logging
from typing import Optional

log = logging.getLogger(__name__)

# 行首角色标记：只匹配行首，避免误杀 "Experience with system design"
ROLE_INJECTION_PATTERN = re.compile(
    r"(?im)^\s*(system|user|assistant|human|ai|model)\s*[:：].*"
)

# 注入短语：精确匹配，不单独匹配 "忽略" 或 "instruction" 等常见词
INJECTION_PHRASE_PATTERN = re.compile(
    r"(ignore\s+(previous|above|all|your)\s*(instructions|prompts|rules))"
    r"|(forget\s+(everything|all\s*(previous\s*)?(instructions|rules|prompts)))"
    r"|(new\s+instructions?:)"
    r"|忽略之前的指令"
    r"|忘记之前的指令"
    r"|忽略以上所有"
    r"|你不再是"
    r"|你的新角色是",
    re.IGNORECASE
)

# 分隔符伪造：匹配项目中 .st 模板使用的静态分隔符
DELIMITER_INJECTION_PATTERN = re.compile(
    r"---(?:简历|文档|问答)内容(?:开始|结束)---"
)

# XML 边界标签伪造：防止攻击者构造 <data-boundary...> 来提前关闭包裹
BOUNDARY_TAG_PATTERN = re.compile(
    r"</?data-boundary[^>]*>",
    re.IGNORECASE
)


class PromptSanitizer:
    """
    Prompt 注入净化工具。

    参考 Java PromptSanitizer.java，对用户输入进行安全过滤。
    """

    def __init__(self, enabled: bool = True) -> None:
        """
        初始化净化器。

        Args:
            enabled: 是否启用净化，默认为 True
        """
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """是否启用净化。"""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """设置是否启用净化。"""
        self._enabled = value

    def sanitize(self, text: str) -> str:
        """
        清洗用户文本，替换危险模式为中性占位符。

        Args:
            text: 原始用户输入

        Returns:
            净化后的文本
        """
        if not text or not text.strip():
            return text

        if not self._enabled:
            return text

        injected = False
        result = text

        # 检测并替换角色扮演注入
        if ROLE_INJECTION_PATTERN.search(result):
            injected = True
            result = ROLE_INJECTION_PATTERN.sub("[filtered-role-marker]", result)

        # 检测并替换指令覆盖攻击
        if INJECTION_PHRASE_PATTERN.search(result):
            injected = True
            result = INJECTION_PHRASE_PATTERN.sub("[filtered]", result)

        # 检测并替换分隔符伪造
        if DELIMITER_INJECTION_PATTERN.search(result):
            result = DELIMITER_INJECTION_PATTERN.sub("[filtered-delimiter]", result)

        # 检测并替换边界标签伪造
        if BOUNDARY_TAG_PATTERN.search(result):
            result = BOUNDARY_TAG_PATTERN.sub("[filtered-boundary-tag]", result)

        if injected:
            log.warning("检测到潜在 Prompt 注入尝试，文本长度: %d", len(text))

        return result

    def wrap_with_delimiters(self, label: str, text: str) -> str:
        """
        用不可预测的分隔符包裹用户文本。

        格式：<data-boundary-{uuid片段}-{label}> ... </data-boundary-{uuid片段}-{label}>
        UUID 片段使攻击者无法提前构造伪造分隔符。

        Args:
            label: 标签（如 "resume", "answer"）
            text: 用户文本

        Returns:
            用分隔符包裹的文本
        """
        uid = uuid.uuid4().hex[:8]
        open_tag = f"<data-boundary-{uid}-{label}>"
        close_tag = f"</data-boundary-{uid}-{label}>"
        return f"{open_tag}\n{text}\n{close_tag}"

    def detect_injection_attempt(self, text: str) -> bool:
        """
        检测注入尝试（仅日志告警，不阻断）。

        Args:
            text: 待检测文本

        Returns:
            是否检测到注入尝试
        """
        if not text or not text.strip():
            return False

        return bool(
            ROLE_INJECTION_PATTERN.search(text)
            or INJECTION_PHRASE_PATTERN.search(text)
        )


# 全局单例（可选使用）
_default_sanitizer: Optional[PromptSanitizer] = None


def get_sanitizer(enabled: bool = True) -> PromptSanitizer:
    """
    获取全局 PromptSanitizer 实例。

    Args:
        enabled: 是否启用

    Returns:
        PromptSanitizer 实例
    """
    global _default_sanitizer
    if _default_sanitizer is None:
        _default_sanitizer = PromptSanitizer(enabled=enabled)
    return _default_sanitizer


def sanitize_user_input(text: str, enabled: bool = True) -> str:
    """
    便捷函数：对用户输入进行净化。

    Args:
        text: 用户输入
        enabled: 是否启用

    Returns:
        净化后的文本
    """
    sanitizer = get_sanitizer(enabled=enabled)
    return sanitizer.sanitize(text)
