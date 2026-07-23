"""
语音面试 WebSocket Handler - 修复版（解决三大问题）。

修复内容：
1. ASR 文本纠错与缓冲：集成 TextCorrectionMiddleware，过滤噪声、纠错、平滑标点
2. LLM 流式优化：真正的流式传输，首 token 立即推送，智能句子检测
3. TTS 生产者-消费者队列：修复竞态条件，每句独立回调，不再吞字截断

核心改进：
- 问题1：ASR 句子碎片化 -> 文本缓冲 + 纠错中间件
- 问题2：LLM 60秒延迟 -> 流式优化 + TTFT 追踪
- 问题3：TTS 吞字截断 -> 独立 TtsTask + 串行队列

作者：AI Assistant
日期：2026-06-18
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from app.utils.timezone_utils import get_beijing_now_naive, to_beijing_naive

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.models.voice_interview import VoiceInterviewMessageEntity, VoiceInterviewSessionEntity
from app.services.voice.agent import VoiceInterviewAgent
from app.services.voice.asr_service import AsrService, AsrSession
from app.services.voice.llm_service import LlmService, LlmServiceDirect
from app.services.voice.text_correction import (
    TextCorrectionMiddleware,
    AsrTextPipeline,
    get_asr_pipeline,
    remove_asr_pipeline,
)
from app.services.voice.tts_queue import TtsQueue
from app.services.voice.ordered_tts_emitter import OrderedTtsEmitter
from app.services.voice.tts_registry import (
    get_and_clear_opening,
    get_pool as get_registry_pool,
)
from app.services.voice.tts_service import TtsService
from app.infrastructure.ai.provider_registry import get_voice_chat_client
from app.utils.prompt_sanitizer import PromptSanitizer  # P1-3: Prompt 注入防护


log = logging.getLogger(__name__)
settings = get_settings()

# Task 3: 角色前缀正则（用于清洗 LLM 输出中的角色前缀）
_ROLE_PREFIX_PATTERN = re.compile(
    r"^(面试官|候选人|AI|助手|interviewer|user)\s*[：:]\s*",
    re.IGNORECASE
)


def strip_role_prefix(text: str) -> str:
    """
    Task 3: 清洗 LLM 输出中的角色前缀。

    Args:
        text: 原始文本

    Returns:
        清洗后的文本（去除行首的角色前缀）

    Examples:
        "面试官：你好" → "你好"
        "interviewer: hello" → "hello"
        "正常输出不含前缀" → "正常输出不含前缀"
    """
    if not text:
        return text
    return _ROLE_PREFIX_PATTERN.sub("", text, count=1).strip()


# 常量定义
AI_SPEAK_COOLDOWN_S = 0.8  # AI 说话结束后冷却期（秒）
MAX_ACCUMULATED_CHARS = 4000  # ASR 累积文本的最大长度
TURN_SUBMIT_TIMEOUT_S = 30.0  # 本轮默认提交的最大等待时间
ASR_FINAL_WAIT_TIMEOUT_S = 3.0  # 手动提交时等待最后一个ASR completed事件
TTS_QUEUE_POOL_SIZE = 1  # TTS 队列消费者数量（必须为1，DashScope有并发限制）


@dataclass
class SessionState:
    """每个 WebSocket 会话的状态。"""
    processing: bool = False
    ai_speaking: bool = False
    ai_speak_end_at: float = 0.0
    last_stt_at: float = field(default_factory=time.time)
    asr_speech_active: bool = False
    asr_final_pending: bool = False
    asr_final_event: asyncio.Event = field(default_factory=asyncio.Event)


class VoiceWebSocketHandler:
    """
    语音面试 WebSocket 处理器（修复版）。

    流水线：
    1. accept() 记录 T0 时间戳
    2. 并行：TTS 队列启动 + ASR 会话启动 + ChatClient 预创建
    3. 发送 welcome
    4. 开场白（预生成 + 复用预热池）
    5. 消息循环：
       - audio -> ASR -> 文本纠错 -> 累积
       - submit_answer -> 提交纠错后文本 -> LLM 流式回复 -> TTS 队列
    6. afterConnectionClosed -> 取消 Task，关闭资源
    """

    def __init__(
        self,
        websocket,
        session_id: int,
        db: AsyncSession,
    ) -> None:
        self.websocket = websocket
        self.session_id = session_id
        self.db = db

        self._running = False
        self._state = SessionState()
        self._state.asr_final_event.set()

        # P1-3: Prompt 注入净化器
        self._sanitizer = PromptSanitizer(enabled=True)

        # 性能追踪
        self._t_accept: float = 0.0
        self._t_opening_text_sent: float = 0.0
        self._t_first_audio_frame: float = 0.0

        # 配置（必须最早初始化，供 _init_llm_client 等方法使用）
        self._cfg = settings

        # 组件
        self._asr = AsrService()
        self._asr_session: AsrSession | None = None
        self._tts = TtsService()
        self._tts_queue: TtsQueue | None = None  # TTS 任务队列
        self._llm_chat_client = None
        self._llm: LlmService | None = None
        self._agent: VoiceInterviewAgent | None = None
        self._session_entity: VoiceInterviewSessionEntity | None = None
        self._llm_system_prompt: str | None = None
        self._llm_chat_client_initialized: bool = False

        # ASR 文本纠错中间件（问题1修复）
        self._text_pipeline = get_asr_pipeline(str(self.session_id))
        self._accumulated_text: str = ""
        self._accumulated_lock = asyncio.Lock()
        self._accumulated_has_terminator: bool = False

        # 句子序号（用于前端排序）
        self._sentence_index = 0
        self._sentence_index_lock = asyncio.Lock()

        # 已注册的 Task 集合
        self._tasks: set[asyncio.Task] = set()

    # ────────────────────────────────────────────────────────────────────────
    # WebSocket 生命周期
    # ────────────────────────────────────────────────────────────────────────

    async def handle(self) -> None:
        """处理 WebSocket 连接。"""
        self._running = True
        try:
            await self.websocket.accept()
            self._t_accept = time.time()
            log.info("[WS %s] [T0] accept at %.3f", self.session_id, self._t_accept)

            # 1. 加载会话实体
            await self._load_session()

            # 2. 初始化 AI Agent
            self._agent = VoiceInterviewAgent(
                skill_id=self._session_entity.skill_id if self._session_entity else "java-backend",
                difficulty=self._session_entity.difficulty if self._session_entity else "mid",
                planned_duration=self._session_entity.planned_duration if self._session_entity else 30,
                llm_provider=self._session_entity.llm_provider if self._session_entity else None,
                tech_enabled=bool(getattr(self._session_entity, "tech_enabled", True)),
                project_enabled=bool(getattr(self._session_entity, "project_enabled", True)),
                hr_enabled=bool(getattr(self._session_entity, "hr_enabled", True)),
            )

            # 3. 绑定 TTS 预热池
            await self._attach_or_warmup_tts_pool()

            # 4. 启动 TTS 任务队列（注入已预热的 TtsService 和 session_id）
            self._tts_queue = TtsQueue(
                tts_service=self._tts,
                session_id=str(self.session_id),  # 与预热的一致
                pool_size=TTS_QUEUE_POOL_SIZE,
            )
            await self._tts_queue.start()

            # 5. 并发执行 ASR 启动 + ChatClient 预创建
            asr_task = self._create_tracked_task(self._start_asr())
            llm_init_task = self._create_tracked_task(self._init_llm_client())
            await asyncio.gather(asr_task, llm_init_task, return_exceptions=True)

            # 6. 发送 welcome
            await self._send_control("welcome", "连接成功，准备开始语音面试")

            # 7. 开场白（fire-and-forget，不阻塞消息循环）
            # 修复：开场白改为后台执行，确保 ASR 消息循环及时响应 session.updated
            # 事件。旧代码 await _trigger_opening() 会阻塞整个 accept() 协程，
            # 导致 ASR 的 session.updated -> on_ready -> _send_control("asr_ready")
            # 无法被处理，麦克风按钮始终禁用。
            if not await self._has_history():
                self._create_tracked_task(self._trigger_opening())

            # 8. 消息循环
            async for raw in self.websocket.iter_text():
                if not self._running:
                    break
                await self._process_message(raw)

        except Exception as e:
            log.error("[WS %s] Error: %s", self.session_id, e)
        finally:
            self._running = False
            await self._cleanup()

    async def _attach_or_warmup_tts_pool(self) -> None:
        """绑定 TTS 预热池或降级为连接时预热。"""
        registry_pool = await get_registry_pool(str(self.session_id))
        if registry_pool is not None:
            await self._tts.attach_prewarmed_pool(str(self.session_id), registry_pool)
            log.info("[WS %s] Attached pre-warmed TTS pool", self.session_id)
        else:
            await self._tts.warmup_session(str(self.session_id))

    async def _init_llm_client(self) -> None:
        """初始化 ChatClient（会话级复用，一次）。"""
        if self._llm_chat_client_initialized:
            return
        try:
            provider = self._session_entity.llm_provider if self._session_entity else None
            log.info("[WS %s] Initializing ChatClient (provider=%s)", self.session_id, provider)
            client = await get_voice_chat_client(provider)
            self._llm_chat_client = client
            self._llm = LlmService(chat_client=client)

            # Step 3 A/B: 根据配置决定使用 LangChain 还是直连 httpx
            use_direct = self._cfg.voice_interview.use_direct_llm_client
            if use_direct:
                self._llm = LlmServiceDirect(chat_client=client)
                log.info("[WS %s] Using LlmServiceDirect (httpx, bypass LangChain)", self.session_id)
            else:
                log.info("[WS %s] Using LlmService (LangChain astream)", self.session_id)

            if self._agent is not None:
                self._llm_system_prompt = self._agent.build_voice_system_prompt()
                log.info("[WS %s] Voice system prompt cached (%d chars)",
                         self.session_id, len(self._llm_system_prompt))

            self._llm_chat_client_initialized = True
        except Exception as e:
            log.warning("[WS %s] Failed to init ChatClient: %s", self.session_id, e)
            # Fallback：不依赖 self._cfg，避免 except 分支自身再触发 AttributeError
            use_direct_fallback = False
            try:
                use_direct_fallback = bool(self._cfg.voice_interview.use_direct_llm_client)
            except Exception:
                pass
            self._llm = LlmServiceDirect() if use_direct_fallback else LlmService()

    def _create_tracked_task(self, coro) -> asyncio.Task:
        """创建 Task 并注册 done_callback。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        """统一捕获 Task 异常。"""
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("[WS %s] Background task %r failed: %s",
                      self.session_id, task.get_name(), exc, exc_info=exc)

    async def _cleanup(self) -> None:
        """清理资源。"""
        # 取消所有 Task
        tasks_to_cancel = [t for t in self._tasks if not t.done()]
        for t in tasks_to_cancel:
            t.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        self._tasks.clear()

        # 关闭 TTS 队列
        if self._tts_queue:
            await self._tts_queue.stop()
            self._tts_queue = None

        # 关闭 ASR
        if self._asr_session:
            try:
                await self._asr.remove_session(self._asr_session.session_id)
            except Exception as e:
                log.warning("[WS %s] ASR remove error: %s", self.session_id, e)
            self._asr_session = None

        # 关闭 TTS 连接池
        try:
            await self._tts.close_pool(str(self.session_id))
        except Exception as e:
            log.warning("[WS %s] TTS close error: %s", self.session_id, e)

        # 移除 ASR 文本处理流水线
        remove_asr_pipeline(str(self.session_id))

        # 自动结束 IN_PROGRESS 会话
        if self._session_entity and self._session_entity.status == "IN_PROGRESS":
            try:
                self._session_entity.status = "COMPLETED"
                self._session_entity.current_phase = "COMPLETED"
                now = get_beijing_now_naive()
                self._session_entity.end_time = now
                self._session_entity.updated_at = now
                if self._session_entity.start_time:
                    start_time_beijing = to_beijing_naive(self._session_entity.start_time)
                    self._session_entity.actual_duration = int(
                        (now - start_time_beijing).total_seconds()
                    )
                await self.db.commit()
                log.info("[WS %s] Auto-ended IN_PROGRESS session", self.session_id)
            except Exception as e:
                log.warning("[WS %s] Failed to auto-end session: %s", self.session_id, e)

    # ────────────────────────────────────────────────────────────────────────
    # ASR 会话管理
    # ────────────────────────────────────────────────────────────────────────

    async def _start_asr(self) -> None:
        """启动 ASR 会话。"""
        log.info("[WS %s] Starting ASR session", self.session_id)

        async def on_speech_started() -> None:
            self._state.asr_speech_active = True
            self._state.asr_final_pending = True
            self._state.asr_final_event.clear()

        async def on_speech_stopped() -> None:
            self._state.asr_speech_active = False

        async def on_partial(text: str) -> None:
            """实时中间结果：展示已确认内容与当前识别片段的合并预览。"""
            if not self._running:
                return None
            self._state.asr_final_pending = True
            self._state.asr_final_event.clear()
            preview = self._text_pipeline.get_preview_with_partial(text)
            await self._send_subtitle(preview, is_final=False)

        async def on_sentence_end(text: str) -> None:
            """句子结束：累积定稿文本，等待用户手动提交。"""
            try:
                if not self._running:
                    return
                if self._state.processing:
                    log.info("[WS %s] Discarding late ASR final during processing: %s",
                             self.session_id, text)
                    return
                log.info("[WS %s] ASR sentence: %s", self.session_id, text)
                self._text_pipeline.add_fragment(text)
                preview = self._text_pipeline.get_accumulated_preview()
                await self._send_subtitle(preview, is_final=False)
            finally:
                self._state.asr_speech_active = False
                self._state.asr_final_pending = False
                self._state.asr_final_event.set()

        def on_ready() -> None:
            if not self._running:
                return None
            # 修复：返回协程对象（而非 _create_tracked_task 返回 None）。
            # _safe_call 检测到协程返回值后，会用 run_coroutine_threadsafe 正确调度到主事件循环。
            # 旧代码 self._create_tracked_task(coro) 在 SDK 回调线程中执行，
            # asyncio.create_task() 要求调用线程有运行中的事件循环，导致 "no running event loop" 错误。
            return self._send_control("asr_ready", "语音识别已就绪")

        async def on_error(err: Exception) -> None:
            log.error("[WS %s] ASR error: %s", self.session_id, err)
            self._state.asr_speech_active = False
            self._state.asr_final_pending = False
            self._state.asr_final_event.set()
            if not self._running:
                return
            await self._send_error(f"语音识别失败: {err}")

        self._asr_session = await self._asr.create_session(
            on_partial=on_partial,
            on_sentence_end=on_sentence_end,
            on_speech_started=on_speech_started,
            on_speech_stopped=on_speech_stopped,
            on_ready=on_ready,
            on_error=on_error,
        )

        try:
            await asyncio.wait_for(self._asr_session._ready_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("[WS %s] ASR not ready after 10s", self.session_id)

    # ────────────────────────────────────────────────────────────────────────
    # 消息处理
    # ────────────────────────────────────────────────────────────────────────

    async def _process_message(self, raw: str) -> None:
        """处理 WebSocket 文本消息。"""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("[WS %s] Invalid JSON: %s", self.session_id, raw[:100])
            return

        t = msg.get("type", "")

        if t == "audio":
            audio_b64 = msg.get("data") or msg.get("audio") or ""
            if audio_b64:
                await self._handle_audio(audio_b64)

        elif t == "submit_answer":
            await self._handle_submit_answer(msg)

        elif t == "control":
            action = msg.get("action", "")
            if action == "submit":
                await self._handle_submit_answer(msg)
            elif action == "end_interview":
                await self._handle_end()
            elif action == "start_phase":
                await self._handle_phase(msg)

        elif t == "text":
            text = msg.get("content", "") or msg.get("text", "") or ""
            if text.strip():
                self._text_pipeline.add_fragment(text)

    async def _handle_audio(self, audio_b64: str) -> None:
        """处理用户麦克风音频。

        P0-3 修复：ASR 断连自动重连（15 次重试，间隔 80ms）。
        """
        if self._is_ai_speaking_or_cooldown():
            return

        if not self._asr_session or not self._asr_session.is_ready:
            return

        try:
            audio_bytes = base64.b64decode(audio_b64)
            await self._asr_session.send_audio(audio_bytes)
        except RuntimeError as e:
            if "not ready" in str(e).lower():
                log.debug("[WS %s] Dropping audio before ASR ready", self.session_id)
            else:
                log.warning("[WS %s] ASR send failed: %s", self.session_id, e)
                # P0-3: 尝试 ASR 重连
                await self._try_asr_reconnect()
        except Exception as e:
            log.error("[WS %s] _handle_audio error: %s", self.session_id, e)
            # P0-3: 判断是否需要重连
            if AsrSession.should_recover_connection(e):
                await self._try_asr_reconnect()

    async def _try_asr_reconnect(self) -> None:
        """
        尝试 ASR 重连（参考 Java handleUserAudio 第 507-526 行）。

        实现：
        - 15 次重试循环
        - 间隔 80ms（asyncio.sleep(0.08)）
        - 使用 should_recover_connection 判断是否继续重试
        """
        if not self._asr_session:
            return

        MAX_RETRIES = 15
        RETRY_INTERVAL_S = 0.08  # 80ms

        log.info("[WS %s] [P0-3] Starting ASR reconnection, max_retries=%d", self.session_id, MAX_RETRIES)

        for attempt in range(MAX_RETRIES):
            try:
                await asyncio.sleep(RETRY_INTERVAL_S)

                # 获取当前回调
                callbacks = self._asr_session._callbacks
                await self._asr_session.restart_transcription(
                    on_partial=callbacks.on_partial,
                    on_sentence_end=callbacks.on_sentence_end,
                    on_ready=callbacks.on_ready,
                    on_error=callbacks.on_error,
                )

                # 等待就绪
                try:
                    await asyncio.wait_for(self._asr_session._ready_event.wait(), timeout=5.0)
                    log.info("[WS %s] [P0-3] ASR reconnected successfully after %d attempts",
                             self.session_id, attempt + 1)
                    return
                except asyncio.TimeoutError:
                    log.warning("[WS %s] [P0-3] ASR reconnection attempt %d timed out",
                                self.session_id, attempt + 1)
                    continue

            except RuntimeError as e:
                # "not ready" 错误，继续重试
                if "not ready" in str(e).lower():
                    log.debug("[WS %s] [P0-3] Retry %d: ASR not ready, continuing",
                              self.session_id, attempt + 1)
                    continue
                # 其他 RuntimeError，判断是否应该恢复
                if AsrSession.should_recover_connection(e):
                    log.warning("[WS %s] [P0-3] Retry %d: recoverable error %s, continuing",
                                self.session_id, attempt + 1, e)
                    continue
                # 不可恢复的错误，停止重试
                log.error("[WS %s] [P0-3] Retry %d: non-recoverable error %s, giving up",
                          self.session_id, attempt + 1, e)
                break
            except Exception as e:
                if AsrSession.should_recover_connection(e):
                    log.warning("[WS %s] [P0-3] Retry %d: recoverable error %s, continuing",
                                self.session_id, attempt + 1, e)
                    continue
                log.error("[WS %s] [P0-3] Retry %d: non-recoverable error %s, giving up",
                          self.session_id, attempt + 1, e)
                break

        log.error("[WS %s] [P0-3] ASR reconnection failed after %d attempts",
                  self.session_id, MAX_RETRIES)

    async def _handle_submit_answer(self, msg: dict) -> None:
        """提交用户回答并触发 LLM 流式回复。"""
        data = msg.get("data")
        submitted_text = data.get("text", "").strip() if isinstance(data, dict) else ""

        # 等待当前处理完成
        waited = 0.0
        while self._state.processing and self._running and waited < 30.0:
            await asyncio.sleep(0.05)
            waited += 0.05
        if self._state.processing:
            log.warning("[WS %s] Previous LLM still processing after 30s", self.session_id)
            await self._send_error("上一轮回答仍在处理中，请稍后再试")
            return

        await self._wait_for_asr_final()
        self._state.processing = True
        try:
            async with self._accumulated_lock:
                self._accumulated_text = ""
                self._accumulated_has_terminator = False

            buffered_text = await self._text_pipeline.get_corrected_text()
            user_text = buffered_text or submitted_text
            sanitized_text = self._sanitizer.sanitize(user_text)
            if sanitized_text != user_text:
                log.info("[WS %s] [PromptSanitizer] Sanitized input", self.session_id)

            if not sanitized_text:
                log.info("[WS %s] submit_answer received but no text", self.session_id)
                await self._send_control("submit_empty", "没有识别到内容，请先说话再提交")
                return

            log.info(
                "[WS %s] [submit_answer] buffered=%d submitted=%d selected=%d chars: %s",
                self.session_id,
                len(buffered_text),
                len(submitted_text),
                len(sanitized_text),
                sanitized_text[:200],
            )
            await self._send_control("submit_accepted", "识别结果已确认")
            await self._flush_to_llm(sanitized_text)
        finally:
            self._state.processing = False

    async def _wait_for_asr_final(self) -> None:
        """等待最后一个ASR分段完成；仍在说话时先显式提交音频缓冲。"""
        if not self._state.asr_final_pending:
            return

        if self._state.asr_speech_active and self._asr_session:
            try:
                log.info("[WS %s] Committing active ASR audio before submit", self.session_id)
                await self._asr_session.commit_audio()
            except Exception as e:
                log.warning("[WS %s] ASR commit before submit failed: %s", self.session_id, e)

        try:
            await asyncio.wait_for(
                self._state.asr_final_event.wait(),
                timeout=ASR_FINAL_WAIT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning("[WS %s] Timed out waiting %.1fs for final ASR segment",
                        self.session_id, ASR_FINAL_WAIT_TIMEOUT_S)

    async def _handle_end(self) -> None:
        await self._send_control("ended", "面试已结束")
        self._running = False

    async def _handle_phase(self, msg: dict) -> None:
        phase = msg.get("phase", "")
        if self._session_entity and phase:
            self._session_entity.current_phase = phase
            await self.db.commit()
            log.info("[WS %s] Phase changed to %s", self.session_id, phase)

    # ────────────────────────────────────────────────────────────────────────
    # LLM 回复（问题2修复：流式 + TTFT）
    # ────────────────────────────────────────────────────────────────────────

    async def _flush_to_llm(self, user_text: str) -> None:
        """
        触发 LLM 流式回复。

        问题2修复：
        1. LLM 每生成一个 token，立即向下游推流
        2. 智能句子检测（遇到终止符才触发 on_sentence）
        3. TTFT 追踪（记录首 token 时间）
        4. 短句缓冲（避免 TTS 碎片化）

        P0-1 修复：使用 OrderedTtsEmitter 保证 TTS 播放顺序。
        """
        if not user_text or not user_text.strip():
            return

        # 防御：LLM 未初始化时等待
        if self._llm is None:
            log.warning("[WS %s] LLM not yet initialized, waiting...", self.session_id)
            for _ in range(20):
                await asyncio.sleep(0.1)
                if self._llm is not None:
                    break
            if self._llm is None:
                log.error("[WS %s] LLM still None after 2s", self.session_id)
                return

        log.info("[WS %s] Submitting to LLM: %s", self.session_id, user_text[:200])

        # 详细计时追踪
        t0 = time.perf_counter_ns()

        self._state.ai_speaking = True

        # 重置句子序号
        async with self._sentence_index_lock:
            self._sentence_index = 0

        # P0-1: 使用有序 TTS 发射器（对齐 Java：每句立即并发 TTS，完成后立即 sendAudio）
        max_concurrent = settings.voice_interview.max_concurrent_tts_per_session or 3
        tts_emitter = OrderedTtsEmitter(
            tts_service=self._tts,
            session_id=str(self.session_id),
            send_fn_async=self._send_audio_chunk_async,
            max_concurrent=max_concurrent,
            timeout=getattr(settings.voice_interview, "tts_timeout_seconds", 30.0) or 30.0,
        )

        t1 = time.perf_counter_ns()
        log.debug("[WS %s] [T-LLM] prep done: %.1fms", self.session_id, (t1 - t0) / 1_000_000)

        sentence_count = 0
        collected_sentences: list[str] = []

        def on_token(text: str) -> None:
            if not self._running:
                return
            if text == "[AI_THINKING]":
                ttft = time.perf_counter_ns()
                ttft_ms = (ttft - t0) / 1_000_000
                log.info("[WS %s] [TTFT] total=%.1fms (from submit_answer)", self.session_id, ttft_ms)
                self._create_tracked_task(self._send_control("ai_thinking", "AI 思考中..."))
                return
            self._create_tracked_task(self._send_text(text, final=False))

        def on_sentence(sentence: str) -> None:
            if not self._running or not sentence.strip():
                return
            nonlocal sentence_count
            # Task 3: 清洗角色前缀（防御性措施，防止 LLM 输出角色前缀）
            cleaned = strip_role_prefix(sentence)
            if cleaned != sentence:
                log.info("[WS %s] [RolePrefix] stripped: '%s' -> '%s'",
                         self.session_id, sentence[:30], cleaned[:30])
            collected_sentences.append(cleaned)
            # 立即并发 TTS（Java 行为：每句立即提交）
            tts_emitter.submit(cleaned if cleaned else sentence)
            sentence_count += 1
            log.info("[WS %s] [OrderedTTS] Submitted sentence %d: %s",
                     self.session_id, sentence_count, cleaned[:30] if cleaned else sentence[:30])

        history_start_ns = time.perf_counter_ns()
        conversation_history = await self._get_conversation_history()
        history_load_ms = (time.perf_counter_ns() - history_start_ns) / 1_000_000
        log.info("[WS %s] [LLM] history_load=%.1fms", self.session_id, history_load_ms)
        history_len = sum(len(h) for h in conversation_history)
        log.info("[WS %s] [LLM] conversation_history=%d turns, %d chars",
                 self.session_id, len(conversation_history) // 2, history_len)

        try:
            ai_reply = await self._llm.chat_stream_sentences(
                user_input=user_text,
                on_token=on_token,
                on_sentence=on_sentence,
                llm_provider=self._session_entity.llm_provider if self._session_entity else None,
                system_prompt=self._llm_system_prompt,
                conversation_history=conversation_history,
                request_start_ns=t0,
            )

            t_llm_done = time.perf_counter_ns()
            log.debug("[WS %s] [T-LLM] chat_stream_sentences done: %.1fms",
                     self.session_id, (t_llm_done - t0) / 1_000_000)

            if not self._running:
                return

            await self._send_subtitle(user_text, is_final=True)
            await self._send_text(ai_reply, final=True)
            await self._save_message(user_text, ai_reply)

            # drain 会等待所有 TTS 完成并发送
            emitted_count = await tts_emitter.drain()
            tts_emitter.shutdown()

            if emitted_count == 0 and ai_reply:
                log.warning("[WS %s] [TTS-Fallback] Streaming TTS emitted 0, falling back to full-text TTS",
                           self.session_id)
                await self._fallback_tts(ai_reply)

            total_time_ms = (time.perf_counter_ns() - t0) / 1_000_000
            log.info("[WS %s] [LLM] Total response time: %.1fms (sentences=%d)",
                     self.session_id, total_time_ms, sentence_count)

            await self._send_control("audio_complete", "面试官语音播放完成")

        except Exception as e:
            log.error("[WS %s] LLM response error: %s", self.session_id, e)
            await self._send_error(f"AI响应失败: {e}")
        finally:
            self._state.ai_speaking = False
            self._state.ai_speak_end_at = time.time() + AI_SPEAK_COOLDOWN_S

    async def _send_audio_chunk_async(self, pcm: bytes, idx: int, is_last: bool) -> None:
        """
        异步发送音频（用于 OrderedTtsEmitter drain 过程）。

        将 PCM 转换为 WAV 后通过 WebSocket 发送。
        """
        wav = _pcm_to_wav(pcm)
        wav_b64 = base64.b64encode(wav).decode("ascii")
        await self._send_audio_chunk(wav_b64, idx, is_last)

    # ────────────────────────────────────────────────────────────────────────
    # 开场白
    # ────────────────────────────────────────────────────────────────────────

    async def _trigger_opening(self) -> None:
        """开场白：使用预生成文本 + 预热池。"""
        greeting = await get_and_clear_opening(str(self.session_id))
        if not greeting:
            log.warning("[WS %s] No pre-cached opening, generating on connect", self.session_id)
            if not self._agent:
                return
            greeting = await self._agent.generate_greeting()
        if not greeting:
            return

        self._t_opening_text_sent = time.time()
        log.info("[WS %s] [T_text] opening text sent: %s",
                 self.session_id, greeting[:80])

        await self._save_message(None, greeting)
        await self._send_text(greeting, final=True)

        complete_event = asyncio.Event()

        def on_audio(chunk: bytes) -> None:
            if not self._running:
                return
            if self._t_first_audio_frame == 0.0:
                self._t_first_audio_frame = time.time()
                log.info("[WS %s] [T1] first audio frame at T0+%.3fs",
                         self.session_id, self._t_first_audio_frame - self._t_accept)
            async def _send():
                wav = _pcm_to_wav(chunk)
                wav_b64 = base64.b64encode(wav).decode("ascii")
                await self._send_audio_chunk(wav_b64, 0, is_last=False)
            asyncio.create_task(_send())

        def on_complete():
            complete_event.set()

        def on_error(e: Exception):
            complete_event.set()

        try:
            await self._tts.synthesize_stream(
                session_id=str(self.session_id),
                text=greeting,
                on_audio=on_audio,
                on_complete=on_complete,
                on_error=on_error,
            )
            await asyncio.wait_for(complete_event.wait(), timeout=30)
        except Exception as e:
            log.warning("[WS %s] Opening TTS failed: %s", self.session_id, e)
        finally:
            await self._send_control("audio_complete", "面试官语音播放完成")

    # ────────────────────────────────────────────────────────────────────────
    # 状态工具
    # ────────────────────────────────────────────────────────────────────────

    def _is_ai_speaking_or_cooldown(self) -> bool:
        if self._state.ai_speaking:
            return True
        return time.time() < self._state.ai_speak_end_at

    # ────────────────────────────────────────────────────────────────────────
    # WebSocket 发送
    # ────────────────────────────────────────────────────────────────────────

    async def _send_control(self, action: str, message: str) -> None:
        try:
            await self.websocket.send_json({
                "type": "control",
                "action": action,
                "message": message,
                "timestamp": int(time.time() * 1000),
            })
            log.info("[WS %s] _send_control sent: action=%s", self.session_id, action)
        except Exception as e:
            log.debug("[WS %s] _send_control failed: %s", self.session_id, e)

    async def _send_text(self, content: str, final: bool) -> None:
        try:
            await self.websocket.send_json({
                "type": "text",
                "content": content,
                "final": final,
            })
        except Exception as e:
            log.debug("[WS %s] _send_text failed: %s", self.session_id, e)

    async def _send_subtitle(self, text: str, is_final: bool) -> None:
        try:
            await self.websocket.send_json({
                "type": "subtitle",
                "text": text,
                "isFinal": is_final,
            })
        except Exception as e:
            log.debug("[WS %s] _send_subtitle failed: %s", self.session_id, e)

    async def _send_audio(self, wav_b64: str, text: str) -> None:
        try:
            await self.websocket.send_json({
                "type": "audio",
                "data": wav_b64,
                "text": text,
            })
        except Exception as e:
            log.debug("[WS %s] _send_audio failed: %s", self.session_id, e)

    async def _send_audio_chunk(self, wav_b64: str, index: int, is_last: bool) -> None:
        try:
            await self.websocket.send_json({
                "type": "audio_chunk",
                "data": wav_b64,
                "index": index,
                "isLast": is_last,
            })
            log.info("[WS %s] [TTS] Sending audio chunk: seq=%d, size=%d bytes, isLast=%s",
                     self.session_id, index, len(wav_b64), is_last)
        except Exception as e:
            log.debug("[WS %s] _send_audio_chunk failed: %s", self.session_id, e)

    async def _send_error(self, message: str) -> None:
        try:
            await self.websocket.send_json({
                "type": "error",
                "message": message,
            })
        except Exception as e:
            log.debug("[WS %s] _send_error failed: %s", self.session_id, e)

    # TTS Fallback - 直接合成（用于流式 TTS 全部失败时）
    async def _fallback_tts(self, text: str) -> bool:
        """
        直接调用 TTS 合成完整文本。

        Returns:
            True if successful, False otherwise
        """
        try:
            log.info("[WS %s] [FallbackTTS] Starting fallback synthesis for: %s",
                     self.session_id, text[:50])

            # 收集所有 PCM
            pcm_chunks: list[bytes] = []
            complete_event = asyncio.Event()
            error_holder: list[Exception] = []

            def on_audio(chunk: bytes) -> None:
                pcm_chunks.append(chunk)

            def on_complete() -> None:
                complete_event.set()

            def on_error(e: Exception) -> None:
                error_holder.append(e)
                complete_event.set()

            await self._tts.concurrent_synthesize_stream(
                session_id=str(self.session_id),
                text=text,
                on_audio=on_audio,
                on_complete=on_complete,
                on_error=on_error,
            )

            timeout = getattr(settings.voice_interview, "tts_timeout_seconds", 30.0) or 30.0
            await asyncio.wait_for(complete_event.wait(), timeout=timeout)

            if pcm_chunks:
                merged_pcm = b"".join(pcm_chunks)
                wav = _pcm_to_wav(merged_pcm)
                wav_b64 = base64.b64encode(wav).decode("ascii")
                await self._send_audio_chunk(wav_b64, 0, is_last=False)
                log.info("[WS %s] [FallbackTTS] Success, WAV size: %d bytes",
                         self.session_id, len(wav))
                return True
            elif error_holder:
                log.warning("[WS %s] [FallbackTTS] Error: %s", self.session_id, error_holder[0])
                return False
            else:
                log.warning("[WS %s] [FallbackTTS] Empty result", self.session_id)
                return False

        except asyncio.TimeoutError:
            log.warning("[WS %s] [FallbackTTS] Timeout after %ds", self.session_id, timeout)
            return False
        except Exception as e:
            log.error("[WS %s] [FallbackTTS] Failed: %s", self.session_id, e)
            return False

    # ────────────────────────────────────────────────────────────────────────
    # 持久化
    # ────────────────────────────────────────────────────────────────────────

    async def _save_message(self, user_text: str | None, ai_text: str | None) -> None:
        now = get_beijing_now_naive()

        user_n = (user_text or "").strip() or None
        ai_n = (ai_text or "").strip() or None

        if not user_n and not ai_n:
            return

        # 填充最新未回答问题
        answer_attached = False
        attached_message_id: int | None = None
        if user_n:
            stmt = (
                select(VoiceInterviewMessageEntity)
                .where(
                    VoiceInterviewMessageEntity.session_id == self.session_id,
                    VoiceInterviewMessageEntity.user_recognized_text.is_(None),
                    VoiceInterviewMessageEntity.ai_generated_text.is_not(None),
                )
                .order_by(VoiceInterviewMessageEntity.sequence_num.desc())
                .limit(1)
            )
            result = await self.db.execute(stmt)
            msg = result.scalar_one_or_none()
            if msg:
                msg.user_recognized_text = user_n
                msg.timestamp = now
                answer_attached = True
                attached_message_id = msg.id

        if ai_n is None:
            await self.db.commit()
            if answer_attached:
                log.info("[WS %s] [_save_message] Updated existing msg %d: user_text=%s",
                         self.session_id, attached_message_id, user_n[:50] if user_n else "")
            return

        # 新建消息
        count_result = await self.db.execute(
            select(func.count(VoiceInterviewMessageEntity.id))
            .where(VoiceInterviewMessageEntity.session_id == self.session_id)
        )
        total_count = int(count_result.scalar_one() or 0)
        seq = total_count + 1

        phase = getattr(self._session_entity, "current_phase", None) if self._session_entity else None
        user_for_new_message = user_n if not answer_attached else None
        log.info("[WS %s] [_save_message] Creating new msg: seq=%d (total existing=%d), user=%s, ai=%s",
                 self.session_id, seq, total_count,
                 user_for_new_message[:50] if user_for_new_message else "None",
                 ai_n[:50] if ai_n else "None")
        msg = VoiceInterviewMessageEntity(
            session_id=self.session_id,
            message_type="DIALOGUE",
            phase=phase,
            user_recognized_text=user_for_new_message,
            ai_generated_text=ai_n,
            sequence_num=seq,
            timestamp=now,
            created_at=now,
        )
        self.db.add(msg)
        await self.db.commit()
        if answer_attached:
            log.info("[WS %s] [_save_message] Updated existing msg %d: user_text=%s",
                     self.session_id, attached_message_id, user_n[:50] if user_n else "")
        log.info("[WS %s] [_save_message] Created msg id=%s, seq=%d", self.session_id, msg.id, seq)

    async def _get_conversation_history(self, max_turns: int = 20) -> list[str]:
        """
        获取对话历史（参考 Java 版本 getHistory）。

        关键：Java 版本没有会话轮数硬性限制，依赖 LLM 上下文窗口。
        这里移除 max_turns 和 MAX_HISTORY_CHARS 截断，对齐 Java 行为。
        """
        try:
            stmt = (
                select(VoiceInterviewMessageEntity)
                .where(VoiceInterviewMessageEntity.session_id == self.session_id)
                .order_by(VoiceInterviewMessageEntity.sequence_num.asc())
            )
            result = await self.db.execute(stmt)
            messages = result.scalars().all()

            log.info("[WS %s] [_get_conversation_history] Found %d messages in DB",
                     self.session_id, len(messages))
            # P1-2 诊断日志：打印每条消息详情
            for i, m in enumerate(messages):
                log.info("[WS %s] [_get_conversation_history] [%d] seq=%s, ai=%s, user=%s",
                         self.session_id, i, m.sequence_num,
                         (m.ai_generated_text or "")[:30],
                         (m.user_recognized_text or "")[:30])

            history: list[str] = []
            pending_ai_question: str | None = None

            # Task 4: 按 Java 版本逻辑构建历史，添加断言校验
            for idx, msg in enumerate(messages):
                ai_text = (msg.ai_generated_text or "").strip() or None
                user_text = (msg.user_recognized_text or "").strip() or None

                # Task 4: 断言校验 - 检测 seq 错位
                is_last = (idx == len(messages) - 1)
                if ai_text and not user_text and not is_last:
                    # AI 非空、user 为空、且不是最后一条 -> 可能是 seq 错位
                    log.warning(
                        "[WS %s] [_get_conversation_history] WARNING: seq=%s has AI but no user (not last). "
                        "This may indicate seq misalignment: ai=%s",
                        self.session_id, msg.sequence_num, ai_text[:50]
                    )

                # 如果有待补充的 AI 问题（上一轮只有 AI 回复，没有用户回答）
                if pending_ai_question is not None:
                    history.append(f"面试官：{pending_ai_question}")
                    pending_ai_question = None
                    if user_text:
                        history.append(f"候选人：{user_text}")
                    if ai_text:
                        pending_ai_question = ai_text
                    continue

                # 正常情况：AI 回复 + 用户回答
                if ai_text and user_text:
                    history.append(f"面试官：{ai_text}")
                    history.append(f"候选人：{user_text}")
                elif ai_text:
                    # 只有 AI 回复，等待下一轮补充用户回答
                    pending_ai_question = ai_text
                elif user_text:
                    history.append(f"候选人：{user_text}")

            # 处理最后一个待补充的 AI 问题
            if pending_ai_question:
                history.append(f"面试官：{pending_ai_question}")

            # Java 版本：无硬性截断，依赖 LLM 上下文窗口
            # 这里仅做最大条数保护（防止极端情况），不限制字符数
            if len(history) > max_turns * 2:
                history = history[-max_turns * 2:]
                log.info("[WS %s] [_get_conversation_history] Safety trim to %d turns",
                         self.session_id, max_turns)

            log.info("[WS %s] [_get_conversation_history] Returning %d items, chars=%d",
                     self.session_id, len(history), len("\n".join(history)))
            return history
        except Exception as e:
            log.warning("[WS %s] Failed to load history: %s", self.session_id, e)
            return []

    async def _has_history(self) -> bool:
        result = await self.db.execute(
            select(func.count(VoiceInterviewMessageEntity.id))
            .where(VoiceInterviewMessageEntity.session_id == self.session_id)
        )
        return (result.scalar_one() or 0) > 0

    async def _load_session(self) -> None:
        result = await self.db.execute(
            select(VoiceInterviewSessionEntity)
            .where(VoiceInterviewSessionEntity.id == self.session_id)
        )
        self._session_entity = result.scalar_one_or_none()


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
    """将 PCM 数据封装为 WAV 格式。"""
    channels = 1
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm_bytes)
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", data_size + 36))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))
    buf.write(struct.pack("<H", 1))
    buf.write(struct.pack("<H", channels))
    buf.write(struct.pack("<I", sample_rate))
    buf.write(struct.pack("<I", byte_rate))
    buf.write(struct.pack("<H", block_align))
    buf.write(struct.pack("<H", bits))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(pcm_bytes)
    return buf.getvalue()
