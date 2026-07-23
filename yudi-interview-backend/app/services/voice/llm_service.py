"""
LLM 流式服务重构版 - 修复 60 秒延迟问题。

问题背景：
1. 响应时间高达 60 秒，严重破坏交互体验
2. 流式 token 检测逻辑有问题：
   - 每次 token 包含标点就立即触发 on_sentence
   - 没有等待完整句子就派发
   - 导致 TTS 合成碎片化、上下文不完整

解决方案：
1. 真正的流式传输：LLM 每生成一个 token，立即向下游推流
2. 智能句子检测：等待完整的句子（遇到终止符）才触发 on_sentence
3. TTFT 优化：
   - 记录首 token 时间
   - 首 token 到达后立即通知前端（显示"AI 思考中"状态）
4. 短句缓冲：避免过短的 TTS 输入导致声学断裂

作者：AI Assistant
日期：2026-06-18
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from app.config.settings import get_settings
from app.infrastructure.ai.provider_registry import get_direct_client, get_voice_chat_client, probe_raw_dashscope

log = logging.getLogger(__name__)
settings = get_settings()

# 句子终止标点定义（中文优先）
_TERMINAL_PUNCTUATION = frozenset("。！？!？")
_COMMA_PUNCTUATION = frozenset("，,")

# 送 TTS 的最小字数阈值
# 过短的文本会导致 TTS 合成时声学上下文不足，产生爆音或机械感
MIN_TTS_CHARS = 8

# 短句最大缓存字符数（防止 LLM 长时间无终止符时无限累积）
SHORT_BUFFER_MAX_CHARS = 60

# 流式句子缓冲最大字符数（超过此值强制 flush，防止句子无限累积）
STREAM_BUFFER_MAX_CHARS = 80

# 实时推送间隔（毫秒）
DEFAULT_EMIT_INTERVAL_MS = 100
DEFAULT_MIN_CHARS_DELTA = 6

# 默认语音面试系统提示词
DEFAULT_VOICE_SYSTEM_PROMPT = """你是一位专业的 AI 面试官，正在与候选人进行实时语音模拟面试。

回答要求：
1. 每轮只问 1 个主问题，必要时最多补 1 个短追问。
2. 长度控制在 2-4 句，避免长段落、列表、Markdown、代码块、表情符号。
3. 使用自然、口语化的中文，避免书面语。
4. 严格基于候选人的上一句回答进行追问或点评，不要重复已经问过的内容。
5. 若候选人回答过短或含糊，直接追问一个具体的技术细节或给出提示引导。
6. 当候选人明确要求换题时，立即切换到新的技术方向。
"""


class LlmService:
    """
    LLM 服务（重构版）。

    核心改进：
    1. 真正的流式：LLM 每生成一个 token，立即向下游推流
    2. 智能句子检测：等待完整的句子才触发 on_sentence
    3. TTFT 优化：首 token 到达后立即通知
    4. 短句缓冲：避免过短的 TTS 输入

    使用方法：
        llm = LlmService(chat_client=client)

        full_text = await llm.chat_stream_sentences(
            user_input="用户说的话",
            on_token=lambda t: send_to_frontend(t),  # 实时文本推送
            on_sentence=lambda s: send_to_tts(s),    # 句子级 TTS
        )
    """

    def __init__(self, chat_client=None) -> None:
        self._cfg = settings.voice_interview
        self._llm_streaming_enabled = True
        # 会话级复用的 ChatClient
        self._chat_client = chat_client
        # 短句缓冲（实例级）
        self._short_buffer: list[str] = []
        # 请求计数（用于追踪每个请求的 instance_id）
        self._req_count = 0

    def set_chat_client(self, chat_client) -> None:
        """动态绑定预初始化的 ChatClient。"""
        self._chat_client = chat_client

    async def chat_stream_sentences(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None,
        on_sentence: Callable[[str], None] | None,
        llm_provider: str | None = None,
        system_prompt: str | None = None,
        conversation_history: list[str] | None = None,
        request_start_ns: int = 0,
    ) -> str:
        """
        流式 LLM，检测句子边界并回调 on_sentence。

        核心算法：
        1. 逐 token 接收 LLM 输出
        2. 实时文本推送（on_token）：每间隔 emit_interval_ms 或累积 min_chars_delta 字符
        3. 句子检测（on_sentence）：遇到终止符（。！？!？）时触发
        4. 短句缓冲：低于 MIN_TTS_CHARS 的句子暂存，与下一句合并

        Args:
            user_input: 用户输入文本
            on_token: 实时文本推送回调
            on_sentence: 句子边界回调（触发 TTS）
            llm_provider: LLM 提供商
            system_prompt: 系统提示词
            conversation_history: 对话历史（参考 Java 版本），格式如 ["面试官：xxx", "候选人：xxx"]
            request_start_ns: 请求开始时间（纳秒），用于精确 TTFT 计算

        Returns:
            完整优化后的文本
        """
        cfg = self._cfg
        emit_interval_ms = max(80, getattr(cfg, 'llm_emit_interval_ms', DEFAULT_EMIT_INTERVAL_MS))
        min_chars_delta = max(4, getattr(cfg, 'llm_min_chars_delta', DEFAULT_MIN_CHARS_DELTA))

        # 如果没有传入 request_start_ns，使用当前时间
        if request_start_ns == 0:
            request_start_ns = time.perf_counter_ns()

        # 状态变量
        raw: list[str] = []
        buffer: list[str] = []  # 当前句子缓冲区
        last_emit_nanos = time.perf_counter_ns()
        last_emit_len = 0
        first_token_nanos = 0
        total_chars = 0

        # 获取 ChatClient
        chat = self._chat_client
        client_init_start = time.perf_counter_ns()

        # Task B1: 记录 ChatClient 实例 ID（用于验证单例复用）
        if chat is not None:
            client_id = id(chat)
            log.info("[LLM] ChatClient instance_id=%d (chat_client attr)", client_id)

        if chat is None:
            try:
                chat = await get_voice_chat_client(llm_provider)
                client_init_ms = (time.perf_counter_ns() - client_init_start) / 1_000_000
                client_id = id(chat)
                log.info("[LLM] [TIMING] Client init: %.1fms | instance_id=%d",
                         client_init_ms, client_id)
            except Exception as e:
                log.error("[LLM] Failed to get chat client: %s", e)
                return self._map_error(e)

        # 构建 messages（参考 Java 版本）
        final_system = (system_prompt or DEFAULT_VOICE_SYSTEM_PROMPT).strip() or DEFAULT_VOICE_SYSTEM_PROMPT

        # 构建 user_prompt（包含对话历史）
        user_parts: list[str] = []
        if conversation_history:
            user_parts.append("【之前的对话】")
            for msg in conversation_history:
                user_parts.append(msg)
            user_parts.append("")
            user_parts.append("【当前对话】")
        user_parts.append(f"用户：{user_input}")
        final_user = "\n".join(user_parts)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": final_system},
            {"role": "user", "content": final_user},
        ]

        total_prompt_chars = len(final_system) + len(final_user)
        # 如果没有传入，使用当前时间
        if request_start_ns == 0:
            request_start_ns = time.perf_counter_ns()
        log.info(
            "[LLM] [TIMING] Request start | system_prompt=%d, user_prompt=%d (history=%d msgs), total=%d chars",
            len(final_system), len(final_user),
            len(conversation_history) if conversation_history else 0,
            total_prompt_chars,
        )
        # 详细日志：显示对话历史内容
        if conversation_history:
            for i, msg in enumerate(conversation_history):
                log.info("[LLM] [HISTORY] [%d] %s", i, msg[:100])

        # P0 TTFT 优化：确保 client 已初始化（ChatClient 在 ws_handler 中已预初始化）
        # 如果是第一次使用，确保 client 可用
        if chat is None:
            try:
                chat = await get_voice_chat_client(llm_provider)
            except Exception as e:
                log.error("[LLM] Failed to get chat client: %s", e)
                return self._map_error(e)

        try:
            # 核心流式循环
            self._req_count += 1
            astream_start = time.perf_counter_ns()
            client_id = id(chat)
            log.info(
                "[LLM] [REQ] instance_id=%d request_no=%d | messages=%d prompt_chars=%d",
                client_id, self._req_count, len(messages), total_prompt_chars,
            )
            log.info("[LLM] [DEBUG] Starting astream: messages_count=%d, total_prompt_chars=%d",
                     len(messages), total_prompt_chars)
            first_chunk = True
            # Task 2 探针：并行发起原始 httpx 请求，对比 LangChain 延迟（不阻塞主流程）
            asyncio.create_task(probe_raw_dashscope())

            # === LangChain astream 阶段细分 ===
            t0 = time.perf_counter_ns()
            gen = chat.astream(messages)
            t1 = time.perf_counter_ns()
            log.info("[LLM] [PHASE] gen_create=%.1fms", (t1 - t0) / 1_000_000)

            aiter = gen.__aiter__()
            t2 = time.perf_counter_ns()
            log.info("[LLM] [PHASE] aiter_init=%.1fms", (t2 - t1) / 1_000_000)

            stream_iter_start_ns = time.perf_counter_ns()
            first_stream_chunk = True
            chunk_seq = 0
            last_chunk_received_ns = stream_iter_start_ns
            log.info(
                "[LLM] [NETWORK] stream_iterator_enter | perf_ns=%d | request_no=%d",
                stream_iter_start_ns, self._req_count,
            )
            async for token in aiter:
                chunk_received_ns = time.perf_counter_ns()
                chunk_seq += 1
                inter_chunk_ms = (chunk_received_ns - last_chunk_received_ns) / 1_000_000
                last_chunk_received_ns = chunk_received_ns
                callback_start_ns = time.perf_counter_ns()
                if first_stream_chunk:
                    first_stream_chunk = False
                    first_stream_chunk_ns = time.perf_counter_ns()
                    log.info(
                        "[LLM] [NETWORK] first_stream_chunk=%.1fms | perf_ns=%d | request_no=%d",
                        (first_stream_chunk_ns - stream_iter_start_ns) / 1_000_000,
                        first_stream_chunk_ns,
                        self._req_count,
                    )
                word = self._extract_content(token)
                if not word:
                    continue

                # 记录首 token 时间（TTFT 指标）
                if first_chunk:
                    t1 = time.perf_counter_ns()
                    first_chunk = False
                    astream_ms = (t1 - astream_start) / 1_000_000
                    ttft_ms = (t1 - request_start_ns) / 1_000_000
                    log.info(
                        "[LLM] [TTFT_BREAKDOWN] "
                        "send_to_first_response=%.1fms | ttft_total=%.1fms | "
                        "prompt_chars=%d | instance_id=%d | request_no=%d",
                        astream_ms, ttft_ms, total_prompt_chars, client_id, self._req_count,
                    )
                    # 通知前端 AI 开始响应
                    if on_token:
                        on_token("[AI_THINKING]")

                raw.append(word)
                total_chars += len(word)

                # === 句子缓冲管理 ===
                buffer.append(word)
                current_sentence = "".join(buffer)

                # === 实时文本推送（on_token）===
                if on_token:
                    now = time.perf_counter_ns()
                    elapsed_ms = (now - last_emit_nanos) / 1_000_000
                    if elapsed_ms >= emit_interval_ms and len("".join(raw)) - last_emit_len >= min_chars_delta:
                        text = self._normalize("".join(raw))
                        if text:
                            on_token(text)
                        last_emit_nanos = now
                        last_emit_len = len(text)

                # === 句子边界检测（on_sentence）===
                should_flush = False

                # 条件1：遇到终止符
                if on_sentence and self._ends_with_terminal(word):
                    should_flush = True

                # 条件2：buffer 超过最大字符数（强制 flush，防止长句子无限累积）
                current_sentence = "".join(buffer)
                if len(current_sentence) >= STREAM_BUFFER_MAX_CHARS:
                    should_flush = True

                if should_flush:
                    sentence = self._normalize(current_sentence)
                    if sentence:
                        self._dispatch_sentence(on_sentence, sentence, buffer)
                    buffer = []  # 重置缓冲区

                callback_end_ns = time.perf_counter_ns()
                log.info(
                    "[LLM] [CHUNK] request_no=%d chunk_seq=%d chunk_received_perf_ns=%d delta_chars=%d "
                    "inter_chunk_ms=%.1f callback_start_perf_ns=%d callback_end_perf_ns=%d",
                    self._req_count,
                    chunk_seq,
                    chunk_received_ns,
                    len(word),
                    inter_chunk_ms,
                    callback_start_ns,
                    callback_end_ns,
                )

        except Exception as e:
            log.error("[LLM] stream error: %s", e)
            # 出错时 flush 短句缓冲
            if on_sentence and buffer:
                self._dispatch_sentence(on_sentence, self._normalize("".join(buffer)), [], force=True)
            self._flush_short_buffer(on_sentence, force=True)
            return self._map_error(e)

        # === 流结束：发送剩余内容 ===
        full = "".join(raw)
        optimized = self._optimize_for_voice(full)

        # 最终文本推送
        if on_token and optimized:
            on_token(optimized)

        # 发送剩余句子（buffer 中可能还有未完成的句子）
        if on_sentence:
            if buffer:
                tail = self._normalize("".join(buffer))
                if tail:
                    self._dispatch_sentence(on_sentence, tail, [], force=True)
            # flush 短句缓冲
            self._flush_short_buffer(on_sentence, force=True)

        total_time_ms = (time.perf_counter_ns() - request_start_ns) / 1_000_000
        log.info(
            "[LLM] [FULL_RESPONSE] text_len=%d, total_time=%.1fms, avg_chars_per_sec=%.0f",
            len(optimized), total_time_ms,
            (len(optimized) / total_time_ms * 1000) if total_time_ms > 0 else 0,
        )
        return optimized

    def _dispatch_sentence(
        self,
        on_sentence: Callable[[str], None],
        sentence: str,
        buffer: list[str],
        force: bool = False,
    ) -> None:
        """
        派发句子到 on_sentence 回调（带短句缓冲）。

        逻辑：
        1. 句子长度 >= MIN_TTS_CHARS：立即派发
        2. 句子长度 < MIN_TTS_CHARS：缓存，与下一句合并
        3. force=True：强制派发（流结束时调用）

        Args:
            on_sentence: 回调函数
            sentence: 待派发的句子
            buffer: 当前缓冲区（用于判断是否需要合并）
            force: 是否强制派发
        """
        s = (sentence or "").strip()
        if not s:
            return

        # 如果缓冲区中有未发送的短句，先合并
        if self._short_buffer:
            merged = "".join(self._short_buffer).strip()
            self._short_buffer.clear()
            if merged:
                # 合并：短的在前，长的在后
                if len(s) >= MIN_TTS_CHARS:
                    s = merged + "，" + s
                else:
                    # 两个都短，继续缓冲
                    self._short_buffer.append(s)
                    s = merged

        if len(s) >= MIN_TTS_CHARS or force:
            log.info("[LLM] [SENTENCE] %s", s)
            try:
                on_sentence(s)
            except Exception as e:
                log.warning("[LLM] on_sentence callback error: %s", e)
        else:
            # 过短，缓存
            self._short_buffer.append(s)
            total = sum(len(x) for x in self._short_buffer)
            if total >= SHORT_BUFFER_MAX_CHARS:
                # 缓冲过长，强制合并派发
                merged = "".join(self._short_buffer).strip()
                self._short_buffer.clear()
                if merged:
                    log.info("[LLM] [SENTENCE] %s", merged)
                    try:
                        on_sentence(merged)
                    except Exception as e:
                        log.warning("[LLM] on_sentence callback error: %s", e)

    def _flush_short_buffer(
        self,
        on_sentence: Callable[[str], None] | None,
        force: bool = False,
    ) -> None:
        """flush 短句缓冲（流结束或出错时调用）。"""
        if not self._short_buffer or not on_sentence:
            return

        merged = "".join(self._short_buffer).strip()
        self._short_buffer.clear()

        if merged and (force or len(merged) >= MIN_TTS_CHARS):
            log.info("[LLM] [SENTENCE] %s", merged)
            try:
                on_sentence(merged)
            except Exception as e:
                log.warning("[LLM] on_sentence callback error: %s", e)

    def _ends_with_terminal(self, token: str) -> bool:
        """判断 token 是否以终止符结尾。"""
        if not token:
            return False
        return token[-1] in _TERMINAL_PUNCTUATION

    def _extract_content(self, chunk) -> str:
        """从 LLM 返回的 chunk 中提取文本内容。"""
        if hasattr(chunk, "content"):
            return str(chunk.content) if chunk.content else ""
        if hasattr(chunk, "text"):
            return str(chunk.text) if chunk.text else ""
        if isinstance(chunk, str):
            return chunk
        return str(chunk) if chunk else ""

    def _normalize(self, text: str) -> str:
        """文本规范化（去除 Markdown、超长截断）。"""
        if not text:
            return ""
        return (
            text.replace("**", "")
            .replace("```", "")
            .replace("`", "")
            .replace("  ", " ")
            .strip()
        )

    def _optimize_for_voice(self, text: str) -> str:
        """优化文本用于语音输出。"""
        normalized = self._normalize(text)
        if not normalized:
            return "请继续。"

        max_chars = max(80, self._cfg.app_voice_ai_question_max_chars)
        if len(normalized) <= max_chars:
            return normalized

        # 在最后一个终止符处截断
        truncated = normalized[:max_chars]
        last_terminal = -1
        for i in range(len(truncated) - 1, -1, -1):
            if truncated[i] in _TERMINAL_PUNCTUATION:
                last_terminal = i
                break

        if last_terminal >= max_chars // 2:
            return truncated[:last_terminal + 1]
        return truncated + "…"

    def _map_error(self, e: Exception) -> str:
        """将异常映射为用户友好的错误消息。"""
        msg = str(e)
        if "403" in msg or "ACCESS_DENIED" in msg or "Authentication" in msg:
            return "AI 服务认证失败，请检查 API Key 配置"
        if "timeout" in msg.lower() or "Timeout" in msg:
            return "AI 服务响应超时，请稍后重试"
        if "429" in msg or "rate limit" in msg.lower() or "quota" in msg.lower():
            return "AI 服务调用频率超限，请稍后重试"
        if "connection" in msg.lower() or "network" in msg.lower():
            return "AI 服务网络连接失败，请检查网络"
        return "抱歉，AI 服务暂时不可用，请稍后重试"

    async def chat(
        self,
        user_input: str,
        llm_provider: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """非流式 LLM 调用（用于不需要实时反馈的场景）。"""
        chat = self._chat_client
        if chat is None:
            try:
                chat = await get_voice_chat_client(llm_provider)
            except Exception as e:
                log.error("[LLM] chat error: %s", e)
                return self._map_error(e)

        try:
            final_system = (system_prompt or DEFAULT_VOICE_SYSTEM_PROMPT).strip() or DEFAULT_VOICE_SYSTEM_PROMPT
            response = await chat.ainvoke([
                {"role": "system", "content": final_system},
                {"role": "user", "content": user_input},
            ])
            text = self._extract_content(response)
            optimized = self._optimize_for_voice(text or "")
            log.info("[LLM] [FULL_RESPONSE] %s", optimized)
            return optimized
        except Exception as e:
            log.error("[LLM] chat error: %s", e)
            return self._map_error(e)


class LlmServiceDirect:
    """
    直接使用 httpx 调用 DashScope API，绕过 LangChain astream 的高初始化延迟。

    LangChain astream 实测 TTFT ~11s，直连 httpx + HTTP/2 实测 ~1s。
    复用 LangChain 的 sentence splitting / short-buffer / optimize 逻辑。
    """

    def __init__(self, chat_client=None) -> None:
        self._cfg = settings.voice_interview
        self._chat_client = chat_client
        self._short_buffer: list[str] = []
        self._req_count = 0

    async def chat_stream_sentences(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None,
        on_sentence: Callable[[str], None] | None,
        llm_provider: str | None = None,
        system_prompt: str | None = None,
        conversation_history: list[str] | None = None,
        request_start_ns: int = 0,
    ) -> str:
        """
        直接使用 httpx 流式请求 DashScope，绕过 LangChain。

        复用 LlmService 的 sentence splitting / short-buffer / optimize 逻辑。
        """
        # 获取 httpx 直连客户端（进程级单例，HTTP/2 复用）
        if request_start_ns == 0:
            request_start_ns = time.perf_counter_ns()
        self._req_count += 1
        log.info(
            "[LLM-DIRECT] [REQ] req_no=%d | history=%d msgs",
            self._req_count,
            len(conversation_history) if conversation_history else 0,
        )

        # 构建消息（与 LlmService 完全一致的格式）
        final_system = (system_prompt or DEFAULT_VOICE_SYSTEM_PROMPT).strip() or DEFAULT_VOICE_SYSTEM_PROMPT
        user_parts: list[str] = []
        if conversation_history:
            user_parts.append("【之前的对话】")
            user_parts.extend(conversation_history)
            user_parts.append("")
            user_parts.append("【当前对话】")
        user_parts.append(f"用户：{user_input}")
        final_user = "\n".join(user_parts)
        messages = [
            {"role": "system", "content": final_system},
            {"role": "user", "content": final_user},
        ]
        total_prompt_chars = len(final_system) + len(final_user)
        client_id = id(self._chat_client) if self._chat_client else 0
        log.info(
            "[LLM-DIRECT] [TIMING] start | prompt_chars=%d | instance_id=%d",
            total_prompt_chars, client_id,
        )

        # 状态变量（复用 LlmService 逻辑）
        raw: list[str] = []
        buffer: list[str] = []
        last_emit_nanos = time.perf_counter_ns()
        last_emit_len = 0
        emit_interval_ms = max(80, getattr(self._cfg, "llm_emit_interval_ms", DEFAULT_EMIT_INTERVAL_MS))
        min_chars_delta = max(4, getattr(self._cfg, "llm_min_chars_delta", DEFAULT_MIN_CHARS_DELTA))
        short_buffer: list[str] = []
        first_chunk = True
        chunk_seq = 0
        last_chunk_received_ns = time.perf_counter_ns()

        try:
            http_client = await get_direct_client(llm_provider)
            payload = {
                "model": settings.ai.provider_dashscope_model or "qwen3.6-flash",
                "messages": messages,
                "stream": True,
            }
            t0 = time.perf_counter_ns()

            async with http_client.stream(
                "POST",
                "/chat/completions",
                json=payload,
            ) as resp:
                t_connect = time.perf_counter_ns()
                log.info(
                    "[LLM-DIRECT] [PHASE] connect=%.1fms",
                    (t_connect - t0) / 1_000_000,
                )
                if resp.status_code != 200:
                    body = await resp.aread()
                    log.error("[LLM-DIRECT] HTTP %d: %s", resp.status_code, body[:200])
                    return self._map_error(RuntimeError(f"HTTP {resp.status_code}"))

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                    except Exception:
                        continue

                    if not delta:
                        continue

                    chunk_received_ns = time.perf_counter_ns()
                    chunk_seq += 1
                    inter_chunk_ms = (chunk_received_ns - last_chunk_received_ns) / 1_000_000
                    last_chunk_received_ns = chunk_received_ns
                    callback_start_ns = time.perf_counter_ns()

                    if first_chunk:
                        first_chunk = False
                        t1 = time.perf_counter_ns()
                        astream_ms = (t1 - t0) / 1_000_000
                        ttft_ms = (t1 - request_start_ns) / 1_000_000
                        log.info(
                            "[LLM-DIRECT] [TTFT] ttft=%.1fms | astream=%.1fms | prompt_chars=%d | req_no=%d",
                            ttft_ms, astream_ms, total_prompt_chars, self._req_count,
                        )
                        if on_token:
                            on_token("[AI_THINKING]")

                    raw.append(delta)
                    buffer.append(delta)
                    total_chars = sum(len(x) for x in raw)

                    # 实时推送
                    if on_token:
                        now = time.perf_counter_ns()
                        elapsed_ms = (now - last_emit_nanos) / 1_000_000
                        if elapsed_ms >= emit_interval_ms and total_chars - last_emit_len >= min_chars_delta:
                            text = self._normalize("".join(raw))
                            if text:
                                on_token(text)
                            last_emit_nanos = now
                            last_emit_len = len(text)

                    # 句子边界检测
                    should_flush = False
                    if on_sentence and self._ends_with_terminal(delta):
                        should_flush = True
                    current_sentence = "".join(buffer)
                    if len(current_sentence) >= STREAM_BUFFER_MAX_CHARS:
                        should_flush = True

                    if should_flush:
                        sentence = self._normalize(current_sentence)
                        if sentence:
                            self._dispatch_sentence(on_sentence, sentence, buffer)
                        buffer = []

                    callback_end_ns = time.perf_counter_ns()
                    log.info(
                        "[LLM-DIRECT] [CHUNK] request_no=%d chunk_seq=%d chunk_received_perf_ns=%d delta_chars=%d "
                        "inter_chunk_ms=%.1f callback_start_perf_ns=%d callback_end_perf_ns=%d",
                        self._req_count,
                        chunk_seq,
                        chunk_received_ns,
                        len(delta),
                        inter_chunk_ms,
                        callback_start_ns,
                        callback_end_ns,
                    )

        except Exception as e:
            log.error("[LLM-DIRECT] stream error: %s", e)
            if on_sentence and buffer:
                self._dispatch_sentence(on_sentence, self._normalize("".join(buffer)), [], force=True)
            self._flush_short_buffer(on_sentence, force=True)
            return self._map_error(e)

        # 流结束
        full = "".join(raw)
        optimized = self._optimize_for_voice(full)
        if on_token and optimized:
            on_token(optimized)
        if on_sentence:
            if buffer:
                tail = self._normalize("".join(buffer))
                if tail:
                    self._dispatch_sentence(on_sentence, tail, [], force=True)
            self._flush_short_buffer(on_sentence, force=True)

        total_time_ms = (time.perf_counter_ns() - request_start_ns) / 1_000_000
        log.info(
            "[LLM-DIRECT] [FULL_RESPONSE] text_len=%d, total_time=%.1fms",
            len(optimized), total_time_ms,
        )
        return optimized

    def _ends_with_terminal(self, token: str) -> bool:
        if not token:
            return False
        return token[-1] in _TERMINAL_PUNCTUATION

    def _dispatch_sentence(
        self,
        on_sentence: Callable[[str], None],
        sentence: str,
        buffer: list[str],
        force: bool = False,
    ) -> None:
        s = (sentence or "").strip()
        if not s:
            return
        if self._short_buffer:
            merged = "".join(self._short_buffer).strip()
            self._short_buffer.clear()
            if merged:
                if len(s) >= MIN_TTS_CHARS:
                    s = merged + "，" + s
                else:
                    self._short_buffer.append(s)
                    s = merged
        if len(s) >= MIN_TTS_CHARS or force:
            log.info("[LLM-DIRECT] [SENTENCE] %s", s)
            try:
                on_sentence(s)
            except Exception as e:
                log.warning("[LLM-DIRECT] on_sentence error: %s", e)
        else:
            self._short_buffer.append(s)
            total = sum(len(x) for x in self._short_buffer)
            if total >= SHORT_BUFFER_MAX_CHARS:
                merged = "".join(self._short_buffer).strip()
                self._short_buffer.clear()
                if merged:
                    log.info("[LLM-DIRECT] [SENTENCE] %s", merged)
                    try:
                        on_sentence(merged)
                    except Exception as e:
                        log.warning("[LLM-DIRECT] on_sentence error: %s", e)

    def _flush_short_buffer(
        self,
        on_sentence: Callable[[str], None] | None,
        force: bool = False,
    ) -> None:
        if not self._short_buffer or not on_sentence:
            return
        merged = "".join(self._short_buffer).strip()
        self._short_buffer.clear()
        if merged and (force or len(merged) >= MIN_TTS_CHARS):
            log.info("[LLM-DIRECT] [SENTENCE] %s", merged)
            try:
                on_sentence(merged)
            except Exception as e:
                log.warning("[LLM-DIRECT] on_sentence error: %s", e)

    def _normalize(self, text: str) -> str:
        if not text:
            return ""
        return (
            text.replace("**", "")
            .replace("```", "")
            .replace("`", "")
            .replace("  ", " ")
            .strip()
        )

    def _optimize_for_voice(self, text: str) -> str:
        normalized = self._normalize(text)
        if not normalized:
            return "请继续。"
        max_chars = max(80, self._cfg.app_voice_ai_question_max_chars)
        if len(normalized) <= max_chars:
            return normalized
        truncated = normalized[:max_chars]
        last_terminal = -1
        for i in range(len(truncated) - 1, -1, -1):
            if truncated[i] in _TERMINAL_PUNCTUATION:
                last_terminal = i
                break
        if last_terminal >= max_chars // 2:
            return truncated[:last_terminal + 1]
        return truncated + "…"

    def _map_error(self, e: Exception) -> str:
        msg = str(e)
        if "403" in msg or "ACCESS_DENIED" in msg:
            return "AI 服务认证失败，请检查 API Key 配置"
        if "timeout" in msg.lower():
            return "AI 服务响应超时，请稍后重试"
        if "429" in msg:
            return "AI 服务调用频率超限，请稍后重试"
        return "抱歉，AI 服务暂时不可用"
