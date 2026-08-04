"""
实时 ASR 服务 - 基于 DashScope SDK OmniRealtimeConversation（与 Java QwenAsrService 完全对应）。

核心改进：
1. 使用 DashScope SDK 的 OmniRealtimeConversation 类，而非手动 websockets.connect
2. SDK 自动处理 WebSocket 握手、认证、协议帧
3. 区分中间结果（in_progress）和句子边界（completed）
4. 每个 completed 事件独立触发文本提交，不累积覆盖
5. VAD 参数通过 update_session 显式配置
6. 音频分片按前端原始 PCM 数据直接发送

DashScope qwen3-asr-flash-realtime 事件序列（VAD 模式）：
1. input_audio_buffer.speech_started   -> 用户开始说话
2. conversation.item.input_audio_transcription.text  -> 实时中间结果（含 text+stash）
3. input_audio_buffer.speech_stopped   -> VAD 检测到句尾
4. conversation.item.input_audio_transcription.completed  -> 最终转写（每句一次）
5. conversation.item.input_audio_transcription.failed    -> 识别失败

参考：
- https://www.alibabacloud.com/help/en/model-studio/qwen-asr-realtime-python-sdk
- Java: interview.guide.modules.voiceinterview.service.QwenAsrService
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

from app.config.settings import get_settings


log = logging.getLogger(__name__)
settings = get_settings()
_ASR_CONNECT_READY_TIMEOUT_SECONDS = 10.0
_ASR_AUDIO_READY_TIMEOUT_SECONDS = 1.2

_FILLER_ONLY_PATTERN = re.compile(
    r"^(?:嗯+|呃+|额+|啊+|哦+|唔+|这个|那个)[，。！？、,.!?\s]*$"
)


def normalize_transcript(text: str, filter_filler_words: bool = True) -> str:
    normalized = text.strip()
    if filter_filler_words and _FILLER_ONLY_PATTERN.fullmatch(normalized):
        return ""
    return normalized


@dataclass
class AsrCallbacks:
    """ASR 回调（与 Java QwenAsrCallback 对应）。"""
    on_partial: Callable[[str], None] = field(default=lambda x: None)
    on_sentence_end: Callable[[str], None] = field(default=lambda x: None)
    on_speech_started: Callable[[], None] = field(default=lambda: None)
    on_speech_stopped: Callable[[], None] = field(default=lambda: None)
    on_ready: Callable[[], None] = field(default=lambda: None)
    on_error: Callable[[Exception], None] = field(default=lambda x: None)


class _AsrCallback:
    """
    OmniRealtimeConversation 的回调，桥接到 AsrCallbacks。

    注意：SDK 回调运行在线程/同步上下文中（无 event loop），
    必须通过 asyncio.run_coroutine_threadsafe 将协程调度回主事件循环执行。
    """

    def __init__(self, callbacks: AsrCallbacks, conn: "AsrSession") -> None:
        self._callbacks = callbacks
        self._conn = conn
        # 延迟获取主循环：在 connect() 中 AsrSession 已持有 _loop，
        # 此处引用用于 _safe_call 的 run_coroutine_threadsafe
        self._handlers = {
            "session.created": self._on_session_created,
            "session.updated": self._on_session_updated,
            "input_audio_buffer.speech_started": self._on_speech_started,
            "input_audio_buffer.speech_stopped": self._on_speech_stopped,
            "conversation.item.input_audio_transcription.text": self._on_partial,
            "conversation.item.input_audio_transcription.delta": self._on_partial,
            "conversation.item.input_audio_transcription.completed": self._on_completed,
            "conversation.item.input_audio_transcription.failed": self._on_failed,
            "session.finished": self._on_session_finished,
            "error": self._on_error,
        }

    def on_open(self) -> None:
        log.debug("[ASR %s] on_open", self._conn._session_id)

    def on_close(self, code, msg) -> None:
        log.debug("[ASR %s] on_close code=%s msg=%s", self._conn._session_id, code, msg)

    def on_event(self, message) -> None:
        """SDK 回调主入口。message 可能是 dict 或 JSON 字符串。"""
        try:
            if isinstance(message, str):
                import json
                msg = json.loads(message)
            else:
                msg = message
        except Exception as e:
            log.warning("[ASR %s] on_event parse error: %s", self._conn._session_id, e)
            return

        t = msg.get("type", "")
        # 诊断：打印每个收到的非音频转写事件（audio_transcription.text 高频，不打印以避免刷屏）
        if t != "conversation.item.input_audio_transcription.text":
            log.info("[ASR %s] event received: type=%s keys=%s", self._conn._session_id, t, list(msg.keys()))
        handler = self._handlers.get(t)
        if handler:
            try:
                handler(msg)
            except Exception as e:
                log.warning("[ASR %s] %s handler error: %s", self._conn._session_id, t, e)
        else:
            log.info("[ASR %s] unhandled event: %s", self._conn._session_id, t)

    def _safe_call(self, fn: Callable, *args, **kwargs) -> None:
        """
        安全调用用户 callback。

        处理两种情况：
        1. fn 是普通同步函数：直接调用，异常记录日志。
        2. fn 是同步包装函数但返回协程/Task（如 on_error = lambda e: _create_tracked_task(...)）
           → 检测返回值类型，使用 run_coroutine_threadsafe 调度到主事件循环。

        这是 SDK 回调线程中跨线程调度的核心机制。
        对应 Java：Spring @Async 线程池执行。
        """
        try:
            result = fn(*args, **kwargs)
            # 检测返回值是否为协程或 Task（on_error 等场景）
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Task):
                loop = getattr(self._conn, "_loop", None)
                if loop is None:
                    log.warning("[ASR %s] No event loop for coroutine return value", self._conn._session_id)
                    return

                def _check_result(f) -> None:
                    try:
                        f.result()
                    except Exception as e:
                        log.error("[ASR %s] async callback failed: %s", self._conn._session_id, e)

                if asyncio.iscoroutine(result):
                    future = asyncio.run_coroutine_threadsafe(result, loop)
                else:
                    # 已经是 Task，直接监听
                    future = result
                future.add_done_callback(_check_result)
        except Exception as e:
            log.warning("[ASR %s] callback error: %s", self._conn._session_id, e)

    def _safe_call_async(self, coro) -> None:
        """
        在 SDK 回调线程中将协程调度到主事件循环执行。

        DashScope OmniRealtimeConversation 的 on_event 回调运行在无 event loop 的线程中，
        不能直接 await 协程。正确做法是用 run_coroutine_threadsafe 将其调度到 AsrSession
        的主事件循环，done_callback 负责异常日志记录（防止 Task exception 未检索）。

        对应 Java：SDK 内部线程回调 → Consumer.accept() → Spring @Async 线程池执行。
        """
        loop = getattr(self._conn, "_loop", None)
        if loop is None:
            log.warning("[ASR %s] No event loop for async callback", self._conn._session_id)
            return

        def _check_result(f) -> None:
            try:
                f.result()
            except Exception as e:
                log.error("[ASR %s] async callback failed: %s", self._conn._session_id, e)

        future = asyncio.run_coroutine_threadsafe(coro, loop)
        future.add_done_callback(_check_result)

    def _on_session_created(self, msg: dict) -> None:
        log.debug("[ASR %s] session.created", self._conn._session_id)

    def _on_session_updated(self, msg: dict) -> None:
        if not self._conn._ready:
            self._conn._ready = True
            loop = self._conn._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._conn._ready_event.set)
            else:
                self._conn._ready_event.set()
            self._safe_call(self._callbacks.on_ready)
        log.debug("[ASR %s] session.updated -> ready", self._conn._session_id)

    def _on_speech_started(self, msg: dict) -> None:
        log.info("[ASR %s] speech started", self._conn._session_id)
        self._safe_call(self._callbacks.on_speech_started)

    def _on_speech_stopped(self, msg: dict) -> None:
        log.info("[ASR %s] speech stopped", self._conn._session_id)
        self._safe_call(self._callbacks.on_speech_stopped)

    def _on_partial(self, msg: dict) -> None:
        text = (msg.get("text") or "") + (msg.get("stash") or "")
        if not text:
            delta = msg.get("delta")
            if isinstance(delta, dict):
                text = delta.get("text") or delta.get("transcript") or ""
            elif isinstance(delta, str):
                text = delta
        text = normalize_transcript(
            text, self._conn._cfg.asr_filter_filler_words
        )
        if text:
            self._safe_call_async(self._callbacks.on_partial(text))

    def _on_completed(self, msg: dict) -> None:
        log.info("[ASR %s] _on_completed called, raw transcript=%r", self._conn._session_id, msg.get("transcript"))
        text = normalize_transcript(
            msg.get("transcript") or "",
            self._conn._cfg.asr_filter_filler_words,
        )
        if text:
            log.info("[ASR %s] Sentence completed: %s", self._conn._session_id, text)
            self._safe_call_async(self._callbacks.on_sentence_end(text))

    def _on_failed(self, msg: dict) -> None:
        log.info("[ASR %s] _on_failed called: %s", self._conn._session_id, msg)
        item_id = msg.get("item_id", "")
        err = msg.get("error", {}) or {}
        err_text = err.get("message", "unknown") if isinstance(err, dict) else str(err)
        log.error("[ASR %s] transcription failed (item=%s): %s", self._conn._session_id, item_id, err_text)
        self._safe_call(self._callbacks.on_error, RuntimeError(f"ASR failed: {err_text}"))

    def _on_session_finished(self, msg: dict) -> None:
        log.debug("[ASR %s] session.finished", self._conn._session_id)

    def _on_error(self, msg: dict) -> None:
        err = msg.get("error", {}) or {}
        err_text = err.get("message", "Unknown") if isinstance(err, dict) else str(err)
        log.error("[ASR %s] error: %s", self._conn._session_id, err_text)
        self._safe_call(self._callbacks.on_error, RuntimeError(err_text))

class AsrSession:
    """
    单个 ASR 会话（基于 OmniRealtimeConversation SDK）。
    """

    def __init__(
        self,
        session_id: str,
        callbacks: AsrCallbacks,
        cfg,
    ) -> None:
        self._session_id = session_id
        self._callbacks = callbacks
        self._cfg = cfg
        self._conversation = None
        self._running = False
        self._ready = False
        self._ready_event = asyncio.Event()
        self._recv_task: asyncio.Task | None = None
        self._sample_rate = self._cfg.asr_sample_rate or 16000
        # 主事件循环引用（供 SDK 回调线程跨线程调度协程使用）
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def connect(self) -> None:
        """建立 OmniRealtimeConversation 连接并配置 VAD 模式。"""
        # 必须在 SDK connect() 之前初始化，以便 on_event 等回调触发时 _loop 已就绪
        self._loop = asyncio.get_running_loop()

        try:
            from dashscope.audio.qwen_omni import OmniRealtimeConversation, MultiModality
            from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams
            import dashscope
        except ImportError as e:
            log.error("[ASR %s] dashscope SDK not installed: %s", self._session_id, e)
            return

        api_key = self._cfg.asr_api_key or settings.ai.bailian_api_key or ""
        if not api_key:
            log.error("[ASR %s] No API key configured", self._session_id)
            return
        dashscope.api_key = api_key

        model = self._cfg.asr_model or "qwen3-asr-flash-realtime"
        url = self._cfg.asr_url
        if not url:
            log.error("[ASR %s] ASR URL not configured (asr_url is empty)", self._session_id)
            return

        log.info(
            "[ASR %s] Connecting to %s | model=%s | format=pcm | rate=%dHz",
            self._session_id, url, model, self._sample_rate,
        )

        callback = _AsrCallback(self._callbacks, self)
        self._conversation = OmniRealtimeConversation(
            model=model,
            url=url,
            callback=callback,
        )

        # connect() 是阻塞调用，必须在线程中执行
        def _connect_blocking() -> None:
            self._conversation.connect()

        await asyncio.to_thread(_connect_blocking)
        self._running = True

        # 配置 session（VAD 模式）
        language = self._cfg.asr_language or "zh"
        threshold = self._cfg.asr_turn_detection_threshold or 0.0
        silence_ms = self._cfg.asr_turn_detection_silence_duration_ms or 2000
        enable_vad = bool(self._cfg.asr_enable_turn_detection)

        transcription_params = TranscriptionParams(
            language=language,
            sample_rate=self._sample_rate,
            input_audio_format=self._cfg.asr_format or "pcm",
        )

        session_cfg = {
            "output_modalities": [MultiModality.TEXT],
            "enable_input_audio_transcription": True,
            "transcription_params": transcription_params,
        }
        if enable_vad:
            session_cfg["enable_turn_detection"] = True
            session_cfg["turn_detection_type"] = self._cfg.asr_turn_detection_type or "server_vad"
            session_cfg["turn_detection_threshold"] = float(threshold)
            session_cfg["turn_detection_silence_duration_ms"] = int(silence_ms)
        else:
            session_cfg["enable_turn_detection"] = False

        log.info(
            "[ASR %s] ===== session.update CONFIG =====\n"
            "  model                  = %s\n"
            "  url                    = %s\n"
            "  sample_rate            = %d Hz\n"
            "  language               = %s\n"
            "  enable_turn_detection  = %s\n"
            "  silence_duration_ms    = %d",
            self._session_id, model, url, self._sample_rate,
            language, enable_vad, silence_ms,
        )

        # update_session 也是阻塞调用
        def _update_blocking() -> None:
            self._conversation.update_session(**session_cfg)

        try:
            await asyncio.to_thread(_update_blocking)
            await asyncio.wait_for(
                self._ready_event.wait(),
                timeout=_ASR_CONNECT_READY_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            await self.close()
            raise RuntimeError(
                f"ASR session ready timeout: {self._session_id}"
            ) from exc
        except Exception:
            await self.close()
            raise

    async def send_audio(self, audio_data: bytes) -> None:
        """发送音频帧到 ASR 服务。"""
        if not self._conversation or not self._running:
            raise RuntimeError(f"ASR session not ready: {self._session_id}")
        if not self._ready:
            try:
                await asyncio.wait_for(
                    self._ready_event.wait(),
                    timeout=_ASR_AUDIO_READY_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"ASR session not ready: {self._session_id}"
                ) from exc

        b64 = base64.b64encode(audio_data).decode("ascii")
        try:
            self._conversation.append_audio(b64)
        except Exception as e:
            log.error("[ASR %s] append_audio error: %s", self._session_id, e)
            raise RuntimeError(f"ASR append failed: {e}") from e

    async def commit_audio(self) -> None:
        """显式提交当前音频缓冲，促使服务端完成最后一个转写分段。"""
        if not self._conversation or not self._running:
            raise RuntimeError(f"ASR session not ready: {self._session_id}")
        await asyncio.to_thread(self._conversation.commit)

    async def close(self) -> None:
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None
        if self._conversation:
            try:
                self._conversation.close()
            except Exception:
                pass
            self._conversation = None
        self._ready = False

    async def restart_transcription(
        self,
        on_partial: Callable[[str], None] | None = None,
        on_sentence_end: Callable[[str], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """重新启动 ASR 会话（参考 Java restartTranscription）。"""
        log.info("[ASR %s] Restarting transcription", self._session_id)
        await self.close()
        if on_partial:
            self._callbacks.on_partial = on_partial
        if on_sentence_end:
            self._callbacks.on_sentence_end = on_sentence_end
        if on_ready:
            self._callbacks.on_ready = on_ready
        if on_error:
            self._callbacks.on_error = on_error
        self._ready = False
        self._ready_event.clear()
        await self.connect()

    @staticmethod
    def should_recover_connection(ex: Exception) -> bool:
        """判断 ASR 错误是否应该触发重连。"""
        try:
            from dashscope.common.error import (
                ServiceUnavailableError,
                TimeoutException,
            )
        except ImportError:
            TimeoutException = ()
            ServiceUnavailableError = ()

        recoverable_types = (
            TimeoutError,
            ConnectionError,
            WebSocketConnectionClosedException,
            WebSocketTimeoutException,
            TimeoutException,
            ServiceUnavailableError,
        )
        non_recoverable_types = (
            AttributeError,
            TypeError,
            KeyError,
            ValueError,
            ImportError,
            LookupError,
            AssertionError,
        )

        # RuntimeError usually preserves the original websocket/SDK exception via
        # __cause__, so inspect the whole chain instead of classifying by outer text.
        seen: set[int] = set()
        current: Exception | None = ex
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, non_recoverable_types):
                return False
            if isinstance(current, recoverable_types):
                return True
            current = current.__cause__

        # The SDK rethrows some websocket errors as a plain Exception. Keep only
        # precise disconnect/timeout messages as a fallback, never "session".
        msg = str(ex).lower()
        recover_messages = (
            "websocket closed due to",
            "connection timed out",
            "connection to remote host was lost",
            "connection is already closed",
            "socket is already closed",
        )
        return any(pattern in msg for pattern in recover_messages)


class AsrService:
    """ASR 服务（会话管理器）。对应 Java QwenAsrService。"""

    def __init__(self) -> None:
        self._cfg = settings.voice_interview
        self._sessions: dict[str, AsrSession] = {}

    async def create_session(
        self,
        on_partial: Callable[[str], None] | None = None,
        on_sentence_end: Callable[[str], None] | None = None,
        on_speech_started: Callable[[], None] | None = None,
        on_speech_stopped: Callable[[], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> AsrSession:
        sid = f"asr_{uuid.uuid4().hex[:8]}"
        callbacks = AsrCallbacks(
            on_partial=on_partial or (lambda x: None),
            on_sentence_end=on_sentence_end or (lambda x: None),
            on_speech_started=on_speech_started or (lambda: None),
            on_speech_stopped=on_speech_stopped or (lambda: None),
            on_ready=on_ready or (lambda: None),
            on_error=on_error or (lambda x: None),
        )
        session = AsrSession(sid, callbacks, self._cfg)
        self._sessions[sid] = session
        await session.connect()
        log.info("[AsrService] Created session: %s", sid)
        return session

    def get_session(self, session_id: str) -> AsrSession | None:
        return self._sessions.get(session_id)

    async def remove_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            await session.close()
            log.info("[AsrService] Removed session: %s", session_id)
