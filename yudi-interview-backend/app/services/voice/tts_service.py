"""
TTS 服务 - 基于 DashScope QwenTtsRealtime 官方 SDK 的流式分片推送。

核心改进（对应 Java QwenTtsService / QwenTtsPool）：
1. 预热连接池：每个会话启动时预热 N 个 TTS 连接，
   消除首次合成时的连接建立延迟（首帧延迟 < 1s）
2. 流式分片推送：每个 response.audio.delta 一经到达立即
   推送给调用方，不得等待 response.done 缓冲完整音频
3. 异步队列桥接：SDK 回调运行在线程中，通过 asyncio.Queue
   桥接到事件循环，避免阻塞
4. 连接复用：池中连接支持多次合成（每次 commit 一次）

参考：
- https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide
- Java: interview.guide.modules.voiceinterview.service.QwenTtsService
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import queue as queue_module
import struct
import threading
import time
import uuid
from typing import Callable

from app.config.settings import get_settings


log = logging.getLogger(__name__)
settings = get_settings()


class TtsConnection:
    """
    单个 TTS 连接（基于官方 QwenTtsRealtime SDK）。

    Task 2: 添加异步重置机制，防止多轮对话后状态漂移。
    response.done 后立即触发异步重置，下次取用时连接已是 ready 状态。

    SDK 在独立线程中接收 WebSocket 消息；通过 asyncio.run_coroutine_threadsafe
    将每个分片安全地派发到事件循环。
    """

    # 连接重置超时（秒）
    RESET_TIMEOUT_SECONDS = 3.0

    def __init__(self, conn_id: str, cfg) -> None:
        self._conn_id = conn_id
        self._cfg = cfg
        self._tts = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready_event = asyncio.Event()
        self._reset_event = asyncio.Event()
        self._ready = False
        self._in_use = False  # 是否正在使用
        self._reuse_count = 0  # 复用次数
        self._reset_task: asyncio.Task | None = None  # 异步重置任务
        # Task A1: 空闲追踪
        self._last_used_at = time.monotonic()
        self._rebuilding = False  # 防止并发重建

    @property
    def is_ready(self) -> bool:
        return self._ready and not self._in_use

    @property
    def reuse_count(self) -> int:
        return self._reuse_count

    async def connect(self) -> None:
        """建立 TTS WebSocket 连接并完成 session.update。"""
        try:
            from dashscope.audio.qwen_tts_realtime import (
                QwenTtsRealtime,
                QwenTtsRealtimeCallback,
                AudioFormat,
            )
            import dashscope
        except ImportError as e:
            log.error("[TTS-conn %s] dashscope SDK not installed: %s", self._conn_id, e)
            return

        api_key = self._cfg.tts_api_key or settings.ai.bailian_api_key or ""
        if not api_key:
            log.error("[TTS-conn %s] No API key configured", self._conn_id)
            return
        dashscope.api_key = api_key

        self._loop = asyncio.get_running_loop()
        callback = _StreamingTtsCallback(self._conn_id, self)

        log.info(
            "[TTS-conn %s] Connecting (model=%s, voice=%s)",
            self._conn_id, self._cfg.tts_model, self._cfg.tts_voice,
        )

        def _connect_blocking() -> None:
            self._tts = QwenTtsRealtime(
                model=self._cfg.tts_model or "qwen3-tts-flash-realtime",
                callback=callback,
                url=self._cfg.tts_url or None,
            )
            self._tts.connect()
            self._tts.update_session(
                voice=self._cfg.tts_voice or "Cherry",
                response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                mode=self._cfg.tts_mode or "commit",
                sample_rate=self._cfg.tts_sample_rate or 24000,
                language_type=self._cfg.tts_language_type or "Chinese",
                speech_rate=self._cfg.tts_speech_rate or 1.0,
                volume=self._cfg.tts_volume or 60,
            )

        # 关键修复：在 to_thread 外层捕获 _connect_blocking 的异常，
        # 确保 update_session() 失败时 ready_event 立即被设置（而非等 10s 超时），
        # 真正异常通过 _schedule_ready_fail 记录到日志。
        exc_in_thread: Exception | None = None
        try:
            await asyncio.to_thread(_connect_blocking)
        except Exception as e:
            exc_in_thread = e

        if exc_in_thread is not None:
            log.error("[TTS-conn %s] connect/update failed: %s", self._conn_id, exc_in_thread)
            self._schedule_ready_fail(exc_in_thread)
            return

        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("[TTS-conn %s] not ready within 10s (update_session may have silently failed)", self._conn_id)

    def _schedule_ready_fail(self, err: Exception) -> None:
        if self._loop and not self._ready_event.is_set():
            self._loop.call_soon_threadsafe(self._ready_event.set)
        # 打印连接失败的详细原因（之前被吞掉了）
        log.error("[TTS-conn %s] Connection failed: %s", self._conn_id, err)

    async def synthesize(
        self,
        text: str,
        on_audio: Callable[[bytes], None],
        on_complete: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """
        提交一次合成，音频分片通过 on_audio 立即推送。

        使用 commit 模式：append_text + commit 触发服务端流式合成。
        多个分片在 SDK 回调线程到达，通过事件循环 on_audio 派发。
        """
        log.info("[TTS-conn %s] synthesize: ready=%s, in_use=%s, tts=%s",
                 self._conn_id, self._ready, self._in_use, self._tts is not None)

        if not self._tts:
            log.error("[TTS-conn %s] synthesize: _tts is None", self._conn_id)
            on_error(RuntimeError(f"TTS connection not initialized: {self._conn_id}"))
            return

        if not self._ready:
            log.error("[TTS-conn %s] synthesize: not ready", self._conn_id)
            on_error(RuntimeError(f"TTS connection not ready: {self._conn_id}"))
            return

        # 注意：in_use 由 acquire() 管理，synthesize() 不重复检查
        log.info("[TTS-conn %s] synthesize: starting commit for text=%s", self._conn_id, text[:50])

        session = _SynthSession(on_audio, on_complete, on_error, self._loop, conn=self)
        self._current_session = session

        def _commit_blocking() -> None:
            try:
                log.info("[TTS-conn %s] append_text + commit", self._conn_id)
                self._tts.append_text(text)
                self._tts.commit()
                log.info("[TTS-conn %s] commit done", self._conn_id)
            except Exception as e:
                log.error("[TTS-conn %s] commit failed: %s", self._conn_id, e)
                session.fire_error(e)

        await asyncio.to_thread(_commit_blocking)
        timeout = self._cfg.tts_timeout_seconds or 30
        try:
            log.info("[TTS-conn %s] waiting for synthesis (timeout=%ss)", self._conn_id, timeout)
            await asyncio.wait_for(session.done_event.wait(), timeout=timeout)
            log.info("[TTS-conn %s] synthesis complete", self._conn_id)
        except asyncio.TimeoutError:
            log.warning("[TTS-conn %s] synthesis timeout after %ss", self._conn_id, timeout)
            session.fire_error(TimeoutError(f"TTS timeout after {timeout}s"))

    def mark_in_use(self) -> None:
        """标记连接为正在使用状态。"""
        self._in_use = True

    async def _handle_disconnect(self, code: int, reason: str) -> None:
        """
        Task A1: 处理连接意外断开（Idle Timeout 或异常关闭）。

        立即标记为 not ready，异步重建连接。
        """
        import time as time_mod
        disconnect_time = time_mod.time()

        if self._rebuilding:
            log.debug("[TTS-conn %s] already rebuilding, skip", self._conn_id)
            return

        self._rebuilding = True
        log.info("[TTS-conn %s] idle timeout detected → rebuilding (code=%s, reason=%s)",
                 self._conn_id, code, reason)

        # 立即标记为不可用（防止被 acquire）
        self._ready = False
        self._in_use = False
        self._ready_event.clear()

        # 关闭旧连接
        await self.close()

        # 异步重建连接
        await self.connect()

        self._rebuilding = False
        log.info("[TTS-conn %s] rebuild done → ready=%s", self._conn_id, self._ready)

    def release(self) -> None:
        """
        释放连接回连接池。

        根据 DashScope qwen3-tts-flash-realtime 协议：
        response.done 后 session 会自动准备好下一次 append_text + commit，
        不需要调用 update_session（调用会导致服务器关闭连接）。
        因此直接标记为 ready。
        """
        self._in_use = False
        self._reuse_count += 1
        # Task A1: 追踪最后使用时间（用于心跳保活）
        self._last_used_at = time.monotonic()

        # session 会自动准备好下一次 commit，直接标记为 ready
        if not self._ready:
            self._ready = True
            if self._loop:
                self._loop.call_soon_threadsafe(self._ready_event.set)

        log.info("[TTS-conn %s] released (reuse_count=%d) → ready",
                 self._conn_id, self._reuse_count)

    async def ping_or_silent_synthesize(self) -> None:
        """
        Task A2: 心跳保活。

        发送极短文本合成保持连接活跃，阻止服务端因 Idle Timeout 断开连接。
        使用中文语气词 "嗯" 作为最小文本（1-2个字足以触发合成）。

        走与正常业务相同的锁定流程：
        mark_in_use → synthesize → release
        """
        import time

        if not self._ready or self._in_use or self._rebuilding:
            return

        t_start = time.perf_counter_ns()
        idle_sec = time.monotonic() - self._last_used_at
        self.mark_in_use()
        log.info("[TTS-conn %s] keepalive: acquired (idle=%.1fs)", self._conn_id, idle_sec)

        try:
            done_event = asyncio.Event()

            def on_audio(chunk: bytes) -> None:
                pass

            def on_complete() -> None:
                done_event.set()

            def on_error(e: Exception) -> None:
                log.warning("[TTS-conn %s] keepalive synthesize error: %s", self._conn_id, e)
                done_event.set()

            await self.synthesize("嗯", on_audio, on_complete, on_error)
            await asyncio.wait_for(done_event.wait(), timeout=5.0)
            elapsed_ms = (time.perf_counter_ns() - t_start) / 1_000_000
            log.info("[TTS-conn %s] keepalive: released (took %.1fms)", self._conn_id, elapsed_ms)
        except Exception as e:
            log.warning("[TTS-conn %s] keepalive failed: %s", self._conn_id, e)
        finally:
            self.release()

    async def _async_reset(self) -> None:
        """
        异步重置连接（方案A）。

        在 response.done 后调用，异步发送 session.update 重新初始化 session。
        重置期间连接不可用，但不会阻塞调用方。
        """
        import time
        reset_start = time.perf_counter_ns()

        log.info("[TTS-conn %s] reset start", self._conn_id)

        if not self._loop:
            log.warning("[TTS-conn %s] _async_reset: no loop", self._conn_id)
            return

        # 标记为未就绪
        self._ready = False
        self._reset_event.clear()

        def _do_reset() -> None:
            try:
                if self._tts:
                    from dashscope.audio.qwen_tts_realtime import AudioFormat
                    self._tts.update_session(
                        voice=self._cfg.tts_voice or "Cherry",
                        response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                        mode=self._cfg.tts_mode or "commit",
                        sample_rate=self._cfg.tts_sample_rate or 24000,
                        language_type=self._cfg.tts_language_type or "Chinese",
                        speech_rate=self._cfg.tts_speech_rate or 1.0,
                        volume=self._cfg.tts_volume or 60,
                    )
                    log.info("[TTS-conn %s] _async_reset: update_session sent", self._conn_id)
                else:
                    log.warning("[TTS-conn %s] _async_reset: no tts object", self._conn_id)
            except Exception as e:
                log.error("[TTS-conn %s] _async_reset: update_session failed: %s", self._conn_id, e)
                self._loop.call_soon_threadsafe(self._reset_event.set)

        await asyncio.to_thread(_do_reset)

        try:
            await asyncio.wait_for(self._reset_event.wait(), timeout=self.RESET_TIMEOUT_SECONDS)
            elapsed_ms = (time.perf_counter_ns() - reset_start) / 1_000_000
            log.info("[TTS-conn %s] reset done (took %.1fms)", self._conn_id, elapsed_ms)
        except asyncio.TimeoutError:
            elapsed_ms = (time.perf_counter_ns() - reset_start) / 1_000_000
            log.warning("[TTS-conn %s] reset timeout after %.1fms → rebuild triggered",
                       self._conn_id, elapsed_ms)
            # 方案B降级：关闭并重建连接
            await self._rebuild_connection()

    async def _rebuild_connection(self) -> None:
        """
        重建 TTS 连接（方案B）。

        当异步重置超时时调用，关闭并重新建立连接。
        """
        import time
        rebuild_start = time.perf_counter_ns()

        log.info("[TTS-conn %s] rebuild triggered", self._conn_id)

        # 关闭旧连接
        await self.close()

        # 重新连接
        await self.connect()

        elapsed_ms = (time.perf_counter_ns() - rebuild_start) / 1_000_000
        if self._ready:
            log.info("[TTS-conn %s] rebuild done (took %.1fms) → ready", self._conn_id, elapsed_ms)
        else:
            log.error("[TTS-conn %s] rebuild failed: not ready after %.1fms",
                      self._conn_id, elapsed_ms)

    def _on_event(self, msg: dict) -> None:
        """
        SDK 回调线程入口。由 _StreamingTtsCallback.on_event 转发。
        重要：response.audio.delta 一经收到立即派发到事件循环，
        不得缓冲到 response.done。
        """
        if not self._loop:
            return
        t = msg.get("type", "")
        session = getattr(self, "_current_session", None)

        if t == "session.updated":
            if not self._ready:
                self._ready = True
                self._loop.call_soon_threadsafe(self._ready_event.set)
            log.info("[TTS-conn %s] session.updated (ready=%s)", self._conn_id, self._ready)

        elif t == "response.audio.delta":
            if not session:
                log.warning("[TTS-conn %s] response.audio.delta but no session", self._conn_id)
                return
            delta = msg.get("delta", "")
            if not delta:
                return
            try:
                chunk = base64.b64decode(delta)
            except Exception as e:
                log.warning("[TTS-conn %s] delta decode failed: %s", self._conn_id, e)
                return
            log.debug("[TTS-conn %s] response.audio.delta: chunk_size=%d bytes, total_count=%d",
                      self._conn_id, len(chunk), session._audio_count + 1)
            # 立即派发到事件循环（流式分片，不缓冲）
            self._loop.call_soon_threadsafe(session.fire_audio, chunk)

        elif t == "response.done":
            log.info("[TTS-conn %s] response.done (session=%s, audio_count=%d)",
                    self._conn_id, session is not None, session._audio_count if session else 0)
            if session:
                self._loop.call_soon_threadsafe(session.fire_complete)

        elif t == "session.finished":
            log.info("[TTS-conn %s] session.finished (session=%s)", self._conn_id, session is not None)
            if session and not session.done_event.is_set():
                self._loop.call_soon_threadsafe(session.fire_complete)

        elif t == "error":
            err = msg.get("error", {}) or {}
            text = err.get("message", "Unknown") if isinstance(err, dict) else str(err)
            log.error("[TTS-conn %s] error: %s", self._conn_id, text)
            if session:
                self._loop.call_soon_threadsafe(session.fire_error, RuntimeError(text))

    async def close(self) -> None:
        def _close() -> None:
            if self._tts:
                try:
                    self._tts.close()
                except Exception:
                    pass
        await asyncio.to_thread(_close)
        self._tts = None
        self._ready = False


class _SynthSession:
    """单次 TTS 合成会话，桥接 SDK 线程与 asyncio 事件循环。"""

    def __init__(
        self,
        on_audio: Callable[[bytes], None],
        on_complete: Callable[[], None],
        on_error: Callable[[Exception], None],
        loop: asyncio.AbstractEventLoop,
        conn: TtsConnection | None = None,
    ) -> None:
        self.on_audio = on_audio
        self.on_complete = on_complete
        self.on_error = on_error
        self.loop = loop
        self.conn = conn  # 引用 TtsConnection，用于触发异步重置
        self.done_event = asyncio.Event()
        self._audio_count = 0

    def fire_audio(self, chunk: bytes) -> None:
        if self.done_event.is_set():
            return
        self._audio_count += 1
        try:
            self.on_audio(chunk)
        except Exception as e:
            log.warning("[TTS-synth] on_audio callback error: %s", e)

    def fire_complete(self) -> None:
        if self.done_event.is_set():
            return
        self.done_event.set()

        # Task 2: 触发连接异步重置
        if self.conn:
            self.loop.call_soon_threadsafe(self.conn.release)

        if self.on_complete:
            try:
                self.on_complete()
            except Exception as e:
                log.warning("[TTS-synth] on_complete callback error: %s", e)

    def fire_error(self, err: Exception) -> None:
        if self.done_event.is_set():
            return
        self.done_event.set()

        # Task 2: 错误时也要触发连接释放（用于重置或重建）
        if self.conn:
            self.loop.call_soon_threadsafe(self.conn.release)

        try:
            self.on_error(err)
        except Exception as e:
            log.warning("[TTS-synth] on_error callback error: %s", e)


class _StreamingTtsCallback:
    """QwenTtsRealtime SDK 回调，桥接到 TtsConnection._on_event。"""

    def __init__(self, conn_id: str, conn: TtsConnection) -> None:
        self._conn_id = conn_id
        self._conn = conn

    def on_open(self) -> None:
        log.debug("[TTS-conn %s] on_open", self._conn_id)

    def on_close(self, code, reason) -> None:
        log.warning("[TTS-conn %s] on_close code=%s reason=%s", self._conn_id, code, reason)
        # Task A1: 检测 Idle Timeout 并触发重建
        reason_str = str(reason) if reason else ""
        is_idle_timeout = "Idle timeout" in reason_str or "timeout" in reason_str.lower()
        is_abnormal_close = code != 1000  # 1000 = Normal closure

        if is_idle_timeout or is_abnormal_close:
            log.warning("[TTS-conn %s] idle timeout / abnormal close detected → triggering rebuild", self._conn_id)
            # 标记连接不可用，触发异步重建
            loop = getattr(self._conn, "_loop", None)
            if loop:
                future = asyncio.run_coroutine_threadsafe(
                    self._conn._handle_disconnect(code, reason_str),
                    loop,
                )
                # 显式调用 result() 将异常重新抛出到日志，防止 "Task exception was never retrieved"
                def _check_result(f: asyncio.Future) -> None:
                    try:
                        f.result()
                    except Exception as e:
                        log.error("[TTS-conn %s] _handle_disconnect failed: %s", self._conn_id, e)

                future.add_done_callback(_check_result)

    def on_event(self, message) -> None:
        try:
            msg = message if isinstance(message, dict) else __import__("json").loads(message)
        except Exception as e:
            log.warning("[TTS-conn %s] on_event parse error: %s", self._conn_id, e)
            return
        try:
            self._conn._on_event(msg)
        except Exception as e:
            log.error("[TTS-conn %s] _on_event error: %s", self._conn_id, e)


class TtsPool:
    """
    单会话 TTS 连接池。预热 N 个连接，异步调度。

    Task 2: 重写调度逻辑：
    1. 只分配 ready=True 且 not in_use 的连接
    2. 连接使用后立即触发异步重置
    3. acquire 时等待连接变为 ready 状态

    Task A2: 心跳保活机制，防止服务端 Idle Timeout 断开连接。
    """

    # 默认连接池大小
    DEFAULT_POOL_SIZE = 5
    # 心跳间隔（秒）= 服务端 Idle Timeout 的 50%
    # DashScope qwen-tts-realtime 服务端 Idle Timeout 约为 30~60 秒
    KEEPALIVE_INTERVAL = 25  # 每 25 秒检查一次空闲连接

    def __init__(self, pool_size: int) -> None:
        self._pool_size = max(1, pool_size or self.DEFAULT_POOL_SIZE)
        self._cfg = settings.voice_interview
        self._connections: list[TtsConnection] = []
        self._started = False
        self._start_lock = asyncio.Lock()
        self._poll_interval = 0.05  # 50ms 轮询间隔
        self._keepalive_task: asyncio.Task | None = None

    async def warmup(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            log.info("[TtsPool] Warming up %d TTS connections (concurrent)", self._pool_size)
            conns = [TtsConnection(f"pool_{i}", self._cfg) for i in range(self._pool_size)]
            # 并发连接所有连接，捕获每个连接的异常以便诊断
            results = await asyncio.gather(
                *(c.connect() for c in conns),
                return_exceptions=True,
            )
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    log.error("[TtsPool] Connection %d raised during warmup: %s", i, result)
            for c in conns:
                self._connections.append(c)
            self._started = True
            ready_count = sum(1 for c in conns if c.is_ready)
            log.info("[TtsPool] Warmup done: %d/%d connections ready", ready_count, len(conns))

            # Task A2: 启动心跳保活
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        """Task A2: 心跳保活循环，防止服务端 Idle Timeout 断开连接。"""
        log.info("[TtsPool] Keepalive loop started (interval=%ds)", self.KEEPALIVE_INTERVAL)
        while self._started:
            try:
                await asyncio.sleep(self.KEEPALIVE_INTERVAL)
                if not self._started:
                    break
                await self._send_keepalive()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("[TtsPool] keepalive loop error: %s", e)
        log.info("[TtsPool] Keepalive loop stopped")

    async def _send_keepalive(self) -> None:
        """Task A2: 对空闲连接发送心跳保活。"""
        for conn in self._connections:
            if not conn.is_ready or conn._in_use or conn._rebuilding:
                continue
            idle_sec = time.monotonic() - conn._last_used_at
            # 超过 KEEPALIVE_INTERVAL 秒未使用的连接，发送心跳
            if idle_sec >= self.KEEPALIVE_INTERVAL:
                try:
                    await conn.ping_or_silent_synthesize()
                except Exception as e:
                    log.warning("[TTS-conn %s] keepalive failed: %s", conn._conn_id, e)

    async def shutdown(self) -> None:
        """Task A2: 关闭连接池，停止心跳。"""
        self._started = False
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
        for conn in self._connections:
            await conn.close()
        self._connections.clear()

    async def acquire(self) -> TtsConnection | None:
        """
        Task 2: 获取一个 ready 的连接。

        轮询等待，直到有 ready 的连接可用。
        返回连接时标记为 in_use。
        """
        import time
        acquire_start = time.perf_counter_ns()
        max_wait_ms = 30000  # 最长等待 30 秒
        poll_count = 0

        while self._running:
            for conn in self._connections:
                poll_count += 1
                if poll_count <= 6:  # 前 6 次打印（每个连接 3 个，共 6 行）
                    log.info("[TtsPool] checking conn %s: is_ready=%s (ready=%s, in_use=%s)",
                             conn._conn_id, conn.is_ready, conn._ready, conn._in_use)
                elif poll_count == 7:
                    log.info("[TtsPool] ... (suppressing further check logs)")
                if conn.is_ready:
                    conn.mark_in_use()
                    waited_ms = (time.perf_counter_ns() - acquire_start) / 1_000_000
                    log.info("[TTS-conn %s] acquired (waited=%.1fms, reuse_count=%d)",
                             conn._conn_id, waited_ms, conn.reuse_count)
                    return conn
            # 检查超时
            waited_ms = (time.perf_counter_ns() - acquire_start) / 1_000_000
            if waited_ms >= max_wait_ms:
                log.error("[TtsPool] acquire timeout after %.1fms (checked %d times), no ready connections",
                          waited_ms, poll_count)
                return None
            # 等待一段时间再重试
            await asyncio.sleep(self._poll_interval)

        return None

    @property
    def _running(self) -> bool:
        return self._started

    async def release(self, conn: TtsConnection) -> None:
        """
        Task 2: 释放连接回池，触发异步重置。
        """
        conn.release()

    async def synthesize_stream(
        self,
        text: str,
        on_audio: Callable[[bytes], None],
        on_complete: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """串行合成：获取连接、执行合成、释放连接。"""
        if not self._started:
            await self.warmup()

        conn = await self.acquire()
        if not conn:
            on_error(RuntimeError("Failed to acquire TTS connection"))
            return

        try:
            await conn.synthesize(text, on_audio, on_complete, on_error)
        finally:
            await self.release(conn)

    async def concurrent_synthesize_stream(
        self,
        text: str,
        on_audio: Callable[[bytes], None],
        on_complete: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """
        并发合成接口。

        获取连接、执行合成、释放连接。
        每个句子使用独立的连接，支持真正的并发。
        """
        if not self._started:
            await self.warmup()

        conn = await self.acquire()
        if not conn:
            on_error(RuntimeError("Failed to acquire TTS connection"))
            return

        try:
            await conn.synthesize(text, on_audio, on_complete, on_error)
        finally:
            await self.release(conn)

    async def close(self) -> None:
        self._started = False
        for c in self._connections:
            await c.close()
        self._connections.clear()


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
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


class TtsService:
    """
    TTS 服务（与 Java QwenTtsService 对应）。

    对外接口：
    - warmup_session(session_id): 并发预热连接池
    - synthesize_stream(session_id, text, on_audio, on_complete, on_error):
        流式合成，分片立即回调
    - synthesize_sync(text) -> bytes: 同步阻塞，完整 PCM
    - synthesize(text) -> str: 同步阻塞，返回 base64 WAV
    - close_pool(session_id): 关闭连接池
    """

    def __init__(self) -> None:
        self._cfg = settings.voice_interview
        self._pools: dict[str, TtsPool] = {}
        self._pools_lock = asyncio.Lock()
        self._warmup_locks: dict[str, asyncio.Lock] = {}

    async def get_pool(self, session_id: str) -> TtsPool:
        # 先检查是否存在（无需锁）
        pool = self._pools.get(session_id)
        if pool is not None:
            return pool

        # 不存在时获取锁并创建
        async with self._pools_lock:
            # 双重检查
            pool = self._pools.get(session_id)
            if pool is not None:
                return pool

            pool_size = self._cfg.max_concurrent_tts_per_session or 3
            pool = TtsPool(pool_size=pool_size)
            self._pools[session_id] = pool
            return pool

    async def get_pool_and_warmup(self, session_id: str) -> TtsPool:
        """获取池并确保已预热（Phase 2 优化）。"""
        pool = await self.get_pool(session_id)

        # 获取或创建池特定的预热锁
        warmup_lock: asyncio.Lock
        async with self._pools_lock:
            if session_id not in self._warmup_locks:
                self._warmup_locks[session_id] = asyncio.Lock()
            warmup_lock = self._warmup_locks[session_id]

        # 使用池特定的锁进行预热（避免死锁）
        async with warmup_lock:
            if not pool._started:
                log.info("[TtsService] Warming up pool for session %s", session_id)
                await pool.warmup()

        return pool

    async def attach_prewarmed_pool(self, session_id: str, pool: TtsPool) -> None:
        """绑定已预热的池（P0-1 优化：复用 POST /sessions 阶段预热的池）。"""
        async with self._pools_lock:
            self._pools[session_id] = pool
            # 已预热的池也设置为 started
            pool._started = True
            # 添加预热锁（已预热，不需要再预热）
            if session_id not in self._warmup_locks:
                self._warmup_locks[session_id] = asyncio.Lock()

    async def warmup_session(self, session_id: str) -> None:
        """预热指定会话的 TTS 连接池。"""
        pool = await self.get_pool(session_id)
        await pool.warmup()
        log.info("[TtsService] Session %s pool warmed up", session_id)

    async def synthesize_stream(
        self,
        session_id: str,
        text: str,
        on_audio: Callable[[bytes], None],
        on_complete: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        if not text or not text.strip():
            on_complete()
            return
        try:
            pool = await self.get_pool_and_warmup(session_id)
            await pool.synthesize_stream(text, on_audio, on_complete, on_error)
        except Exception as e:
            log.error("[TtsService] synthesize_stream error: %s", e)
            try:
                on_error(e)
            except Exception:
                pass

    async def concurrent_synthesize_stream(
        self,
        session_id: str,
        text: str,
        on_audio: Callable[[bytes], None],
        on_complete: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """
        Phase 2 新增：并发合成接口。

        使用独立的 TtsConnection，支持真正的多连接并发。
        用于多句 LLM 输出并行 TTS 场景。
        """
        if not text or not text.strip():
            on_complete()
            return
        try:
            # 使用 get_pool_and_warmup 确保池已预热
            pool = await self.get_pool_and_warmup(session_id)
            await pool.concurrent_synthesize_stream(text, on_audio, on_complete, on_error)
        except Exception as e:
            log.error("[TtsService] concurrent_synthesize_stream error: %s", e)
            try:
                on_error(e)
            except Exception:
                pass

    def synthesize_sync(self, text: str, timeout: float = 30) -> bytes:
        """同步合成（阻塞），仅供非异步上下文使用（如启动期预热）。"""
        if not text or not text.strip():
            return b""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # 当前已在事件循环中，不能 asyncio.run()，改用 run_in_executor 兜底
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(asyncio.run, self._synthesize_async(text, timeout))
                return fut.result(timeout=timeout + 5)
        return asyncio.run(self._synthesize_async(text, timeout))

    async def _synthesize_async(self, text: str, timeout: float = 30) -> bytes:
        chunks: list[bytes] = []
        complete = asyncio.Event()
        err: list[Exception] = []

        def on_audio(chunk: bytes) -> None:
            chunks.append(chunk)

        def on_complete() -> None:
            complete.set()

        def on_error(e: Exception) -> None:
            err.append(e)
            complete.set()

        await self.synthesize_stream("__sync__", text, on_audio, on_complete, on_error)
        try:
            await asyncio.wait_for(complete.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        if err:
            raise err[0]
        return b"".join(chunks)

    def synthesize(self, text: str) -> str:
        pcm = self.synthesize_sync(text)
        if not pcm:
            return ""
        return base64.b64encode(_pcm_to_wav(pcm, self._cfg.tts_sample_rate or 24000)).decode("ascii")

    def pcm_to_wav_base64(self, pcm_bytes: bytes, sample_rate: int = 24000) -> str:
        return base64.b64encode(_pcm_to_wav(pcm_bytes, sample_rate)).decode("ascii")

    async def close_pool(self, session_id: str) -> None:
        async with self._pools_lock:
            pool = self._pools.pop(session_id, None)
        if not pool:
            return
        # If the pool was pre-warmed via the registry, the registry owns its lifecycle.
        # Skip close here to avoid double-close; the registry closes it on session end.
        from app.services.voice.tts_registry import get_pool as _reg_get
        if await _reg_get(session_id) is not None:
            return
        await pool.close()
