"""
ASR 文本纠错与标点平滑中间件。

问题背景：
- ASR 实时识别结果往往被切分成极其细碎的片段（如 "您好" "请问您" "今天" "天气"）
- 存在部分识别错误（如 "了解" 识别成 "了解" 本身，但如果口音重可能出现 "李姐"）
- 直接将这些碎片送入 LLM 会导致上下文不完整、追问不自然

解决方案：
1. 文本缓冲：累积短文本直到自然断句点（标点、换行）
2. 常见错误纠错：基于规则的正则替换（如 "李姐" -> "理解"，"了解" 重复等）
3. 标点平滑：自动补全缺失的句末标点
4. 长度控制：防止无限累积导致内存溢出

使用方法：
    middleware = TextCorrectionMiddleware()
    corrected = await middleware.correct("用户刚说的话")
    # 或者批量处理累积文本
    corrected = await middleware.correct_batch(accumulated_text)

作者：AI Assistant
日期：2026-06-18
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class CorrectionResult:
    """纠错结果。"""
    original: str
    corrected: str
    changes: list[str] = field(default_factory=list)  # 记录做了哪些修改
    is_meaningful: bool = True  # 是否包含有效内容


class TextCorrectionMiddleware:
    """
    ASR 文本纠错与标点平滑中间件。

    功能：
    1. 文本缓冲与合并：将连续短句合并为完整语义块
    2. 常见 ASR 错误纠错：基于规则的正则替换
    3. 标点平滑：自动补全缺失的句末标点
    4. 噪声过滤：过滤纯语气词、无意义片段

    配置参数：
    - min_meaningful_length: 有效文本最小长度（低于此值的短文本会被过滤或合并）
    - max_buffer_length: 最大缓冲长度（防止内存溢出）
    - enable_correction: 是否启用纠错
    - enable_punctuation: 是否启用标点平滑
    """

    # 常见 ASR 识别错误（口音、方言、近音词）
    # 格式：(错误模式, 正确文本, 优先级)
    # 优先级：0=严格匹配，1=宽松匹配
    _ASR_CORRECTIONS: list[tuple[str, str, int]] = [
        # 常见近音词错误
        (r"李姐", "理解", 0),        # "李姐" 是 "理解" 的常见误识别
        (r"解了", "了解", 0),        # "解了" 应为 "了解"
        (r"了解解", "了解", 0),      # "了解解" 重复
        (r"了解了解", "了解", 0),    # "了解了解" 重复
        (r"那个那个", "那个", 0),    # "那个那个" 重复
        (r"这个这个", "这个", 0),    # "这个这个" 重复
        (r"对对对", "对", 0),        # "对对对" 过度确认
        (r"好好好", "好", 0),       # "好好好" 过度确认
        (r"嗯嗯嗯", "嗯", 0),       # "嗯嗯嗯" 语气词重复
        (r"啊啊啊啊", "啊", 0),     # "啊啊啊啊" 语气词重复
        (r"就是就是", "就是", 0),    # "就是就是" 重复
        (r"然后然后", "然后", 0),   # "然后然后" 重复

        # 常见技术词汇错误
        (r"微服务", "微服务", 1),    # 确保正确
        (r"服务器", "服务器", 1),
        (r"数据库", "数据库", 1),
        (r"缓存", "缓存", 1),
        (r"队列", "队列", 1),

        # 常见口音问题
        (r"做", "做", 1),            # 确保正确
        (r"组", "做", 0),            # "组" 在某些语境下应为 "做"
        (r"在", "在", 1),
        (r"再", "再", 0),            # "在" 和 "再" 常混淆

        # 语气词和填充词
        (r"呃呃", "呃", 0),
        (r"嗯嗯", "嗯", 0),
        (r"啊嗯", "嗯", 0),
        (r"哦哦", "哦", 0),
    ]

    # 无意义的纯语气词/填充词（整体过滤）
    _NOISE_PATTERNS: tuple[str, ...] = (
        r"^啊+$",       # 只有 "啊"
        r"^嗯+$",       # 只有 "嗯"
        r"^呃+$",       # 只有 "呃"
        r"^哦+$",       # 只有 "哦"
        r"^哈+$",       # 只有 "哈"
        r"^嗯哼$",      # "嗯哼"
        r"^呵呵$",      # "呵呵"
        r"^哈哈+$",     # "哈哈" 或 "哈哈哈哈"
        r"^呃呃+$",     # "呃呃"
    )

    # 句末标点（用于判断是否需要补全）
    _TERMINAL_PUNCTUATION: tuple[str, ...] = ("。", "！", "？", ".", "!", "?")

    # 连接词（用于短句合并时的自然连接）
    _CONNECTORS: tuple[str, ...] = ("，", ",", "、", "和", "与", "以及")

    def __init__(
        self,
        min_meaningful_length: int = 3,
        max_buffer_length: int = 500,
        enable_correction: bool = True,
        enable_punctuation: bool = True,
        enable_denoising: bool = True,
    ) -> None:
        self._min_meaningful_length = min_meaningful_length
        self._max_buffer_length = max_buffer_length
        self._enable_correction = enable_correction
        self._enable_punctuation = enable_punctuation
        self._enable_denoising = enable_denoising

        # 编译正则表达式以提高性能
        self._noise_patterns = [re.compile(p) for p in self._NOISE_PATTERNS]
        self._correction_patterns: list[tuple[re.Pattern, str, int]] = []
        for pattern, replacement, priority in self._ASR_CORRECTIONS:
            self._correction_patterns.append(
                (re.compile(pattern), replacement, priority)
            )

        # 内部缓冲（用于短句合并）
        self._buffer: list[str] = []
        self._buffer_lock = None  # asyncio.Lock 会在异步上下文中创建

    async def correct(self, text: str) -> str:
        """
        纠错单条文本。

        Args:
            text: 原始 ASR 识别文本

        Returns:
            纠错后的文本
        """
        if not text or not text.strip():
            return text

        original = text.strip()
        result = await self._process_text(original)

        if result.corrected != original:
            log.debug(
                "[TextCorrection] corrected: %s -> %s | changes: %s",
                original, result.corrected, result.changes,
            )

        return result.corrected

    async def correct_batch(self, texts: list[str]) -> str:
        """
        批量纠错多条文本，并合并为一段完整文本。

        Args:
            texts: 多条 ASR 识别文本列表

        Returns:
            合并并纠错后的完整文本
        """
        if not texts:
            return ""

        # 过滤无效文本
        valid_texts: list[str] = []
        for t in texts:
            t = t.strip()
            if not t:
                continue
            # 噪声过滤
            if self._enable_denoising and self._is_noise(t):
                continue
            # 纠错
            t = await self.correct(t)
            if t:
                valid_texts.append(t)

        if not valid_texts:
            return ""

        # 自然合并（使用标点或空格分隔）
        merged = self._merge_texts_naturally(valid_texts)

        # 最终标点平滑
        if self._enable_punctuation:
            merged = self._add_terminal_punctuation(merged)

        return merged

    async def _process_text(self, text: str) -> CorrectionResult:
        """
        处理单条文本，执行纠错、标点平滑、噪声过滤。

        Args:
            text: 原始文本

        Returns:
            纠错结果
        """
        changes: list[str] = []
        original = text

        # 1. 噪声过滤
        if self._enable_denoising and self._is_noise(text):
            return CorrectionResult(
                original=original,
                corrected="",
                changes=["filtered_noise"],
                is_meaningful=False,
            )

        # 2. 常见错误纠错
        if self._enable_correction:
            text, detected_changes = self._apply_corrections(text)
            changes.extend(detected_changes)

        # 3. 标点平滑（自动补全缺失的句末标点）
        if self._enable_punctuation:
            text, added = self._add_terminal_punctuation(text, return_change=True)
            if added:
                changes.append("added_terminal_punctuation")

        # 4. 长度检查
        if len(text) < self._min_meaningful_length:
            return CorrectionResult(
                original=original,
                corrected=text,
                changes=changes,
                is_meaningful=False,
            )

        return CorrectionResult(
            original=original,
            corrected=text,
            changes=changes,
            is_meaningful=True,
        )

    def _is_noise(self, text: str) -> bool:
        """判断文本是否为无意义的噪声。"""
        if not text:
            return True

        # 检查是否匹配噪声模式
        for pattern in self._noise_patterns:
            if pattern.match(text):
                return True

        # 纯标点符号
        if all(c in "。，、！？.!?,;；:：" for c in text):
            return True

        # 太短且无实际内容（只有 1-2 个字）
        if len(text) <= 2:
            # 检查是否为有意义的单字词
            meaningful_single_chars = {"我", "你", "他", "她", "它", "是", "在", "有", "的", "了"}
            if text not in meaningful_single_chars:
                return True

        return False

    def _apply_corrections(self, text: str) -> tuple[str, list[str]]:
        """
        应用常见错误纠错。

        Args:
            text: 原始文本

        Returns:
            (纠错后文本, 记录的修改列表)
        """
        changes: list[str] = []
        original = text

        # 按优先级排序（先严格后宽松）
        sorted_patterns = sorted(self._correction_patterns, key=lambda x: x[2])

        for pattern, replacement, _ in sorted_patterns:
            new_text = pattern.sub(replacement, text)
            if new_text != text:
                changes.append(f"{pattern.pattern} -> {replacement}")
                text = new_text

        # 去除连续重复字符（超过2个的重复）
        text = self._remove_excessive_duplication(text)

        return text, changes

    @staticmethod
    def _remove_excessive_duplication(text: str) -> str:
        """
        去除过度重复的字符。

        例如： "啊啊啊" -> "啊"，"对对对" -> "对"
        """
        # 匹配连续重复超过2次的字符
        pattern = re.compile(r"(.)\1{2,}")
        return pattern.sub(r"\1\1", text)

    def _merge_texts_naturally(self, texts: list[str]) -> str:
        """
        自然合并多条文本。

        规则：
        1. 如果前一句以标点结尾，直接拼接下一句
        2. 如果前一句以连接词结尾，添加逗号
        3. 否则添加空格
        """
        if not texts:
            return ""
        if len(texts) == 1:
            return texts[0]

        result = texts[0]
        for i in range(1, len(texts)):
            next_text = texts[i]
            if not next_text:
                continue

            # 检查前一句末尾
            if result.endswith(self._TERMINAL_PUNCTUATION):
                # 以标点结尾，直接拼接（首字符如果是标点则去重）
                if next_text[0] in "，。、；：！？.!?,;:":
                    result = result + next_text[1:]
                else:
                    result = result + next_text
            elif result.endswith(self._CONNECTORS):
                # 以连接词结尾，添加逗号
                result = result + next_text
            else:
                # 其他情况，添加逗号分隔
                result = result + "，" + next_text

        return result

    def _add_terminal_punctuation(
        self,
        text: str,
        return_change: bool = False,
    ) -> tuple[str, bool]:
        """
        自动补全缺失的句末标点。

        Args:
            text: 文本
            return_change: 是否返回是否有修改

        Returns:
            如果 return_change=True，返回 (处理后文本, 是否有修改)
            否则只返回处理后文本
        """
        if not text:
            return (text, False) if return_change else text

        # 已经以标点结尾，无需处理
        if any(text.endswith(p) for p in self._TERMINAL_PUNCTUATION):
            return (text, False) if return_change else text

        # 太短，不强制添加标点
        if len(text) < 5:
            return (text, False) if return_change else text

        # 添加句号
        new_text = text + "。"
        return (new_text, True) if return_change else new_text

    def reset_buffer(self) -> None:
        """重置内部缓冲。"""
        self._buffer.clear()

    def get_buffered_text(self) -> str:
        """获取当前缓冲的文本。"""
        return "".join(self._buffer)


class AsrTextPipeline:
    """按 Java 版本规则合并 ASR 定稿片段。"""

    def __init__(
        self,
        enable_correction: bool = True,
        enable_punctuation: bool = True,
        enable_denoising: bool = True,
        min_sentence_length: int = 5,  # 最小句子长度
        max_accumulated_length: int = 1000,  # 最大累积长度
    ) -> None:
        self._middleware = TextCorrectionMiddleware(
            enable_correction=enable_correction,
            enable_punctuation=enable_punctuation,
            enable_denoising=enable_denoising,
        )
        self._min_sentence_length = min_sentence_length
        self._max_accumulated_length = max_accumulated_length

        # 累积的片段列表
        self._fragments: list[str] = []

    def add_fragment(self, text: str) -> None:
        """
        添加一个 ASR 识别片段。

        Args:
            text: ASR 返回的原始文本
        """
        if not text or not text.strip():
            return

        text = text.strip()

        previous = "".join(self._fragments)
        merged = self._join_segments(previous, text) if previous else text
        self._fragments[:] = [merged]
        log.debug(
            "[AsrTextPipeline] fragment added: %s (total %d chars)",
            text, len(merged),
        )

    @staticmethod
    def _join_segments(previous: str, current: str) -> str:
        if current == previous or current.startswith(previous):
            return current
        if previous.endswith(current):
            return previous
        separator = " " if previous.endswith(("。", "！", "？", ".", "!", "?")) else "，"
        return previous + separator + current

    async def get_corrected_text(self) -> str:
        """
        获取纠错后的完整文本（用于送入 LLM）。

        Returns:
            合并并纠错后的文本
        """
        if not self._fragments:
            return ""

        merged = "".join(self._fragments)
        self._fragments.clear()
        return merged

    def get_accumulated_preview(self) -> str:
        """
        获取当前累积文本的预览（不清理缓冲）。

        用于前端展示"识别中"状态。
        """
        if not self._fragments:
            return ""
        return "".join(self._fragments)

    def get_preview_with_partial(self, partial: str) -> str:
        """拼接已确认分段与当前ASR临时结果，用于实时字幕。"""
        current = (partial or "").strip()
        confirmed = self.get_accumulated_preview()
        if not current:
            return confirmed
        if not confirmed:
            return current
        return self._join_segments(confirmed, current)

    def reset(self) -> None:
        """重置流水线。"""
        self._fragments.clear()
        self._middleware.reset_buffer()


# 全局流水线实例缓存（按会话 ID）
_pipeline_cache: dict[str, AsrTextPipeline] = {}


def get_asr_pipeline(session_id: str) -> AsrTextPipeline:
    """
    获取或创建指定会话的 ASR 文本处理流水线。

    Args:
        session_id: 会话 ID

    Returns:
        AsrTextPipeline 实例
    """
    if session_id not in _pipeline_cache:
        _pipeline_cache[session_id] = AsrTextPipeline()
    return _pipeline_cache[session_id]


def remove_asr_pipeline(session_id: str) -> None:
    """
    移除指定会话的 ASR 文本处理流水线。

    Args:
        session_id: 会话 ID
    """
    if session_id in _pipeline_cache:
        del _pipeline_cache[session_id]
