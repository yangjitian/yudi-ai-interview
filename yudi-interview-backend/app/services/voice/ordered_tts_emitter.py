"""
有序 TTS 发射器 - 参考 Java OrderedTtsChunkEmitter 实现。

Java 行为：
1. 每收到完整句子立即并发 TTS
2. 用多线程 + CountDownLatch 等待
3. TTS 完成后立即 sendAudio（按序）

Python 实现：
1. 每句创建并发 TTS 任务
2. 每个任务完成后立即 sendAudio
3. 使用 asyncio.Semaphore 控制并发度
4. 按 index 顺序发送，保证播放顺序
5. 添加发送间隔控制，防止 chunk 爆发发送
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# 每个 chunk 发送后的最小间隔（毫秒），防止爆发发送
CHUNK_SEND_INTERVAL_MS = 20


@dataclass
class _TtsTask:
    """单个 TTS 任务的结果。"""
    index: int
    text: str
    chunks: list[bytes]
    error: Exception | None = None


class OrderedTtsEmitter:
    """
    有序 TTS 发射器（对齐 Java OrderedTtsChunkEmitter）。

    关键设计：
    1. submit() 立即启动 TTS（并发）
    2. 每个 TTS 任务完成后立即 sendAudio
    3. 按 index 顺序发送，保证播放顺序
    4. drain() 等待所有任务完成
    """

    def __init__(
        self,
        tts_service,
        session_id: str,
        send_fn_async: Callable[[bytes, int, bool], Awaitable],
        max_concurrent: int = 3,
        timeout: float = 30.0,
    ) -> None:
        self._tts_service = tts_service
        self._session_id = session_id
        self._send_fn_async = send_fn_async
        self._max_concurrent = max(1, max_concurrent)
        self._timeout = timeout

        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._next_index = 0
        self._total_submitted = 0

        # 任务结果存储
        self._tasks: dict[int, asyncio.Task] = {}
        self._results: dict[int, _TtsTask] = {}
        self._lock = asyncio.Lock()

        # 发送状态
        self._sent_index = 0
        self._finished = False
        self._last_send_time = 0.0  # 上次发送时间（纳秒）

    def submit(self, text: str) -> asyncio.Task:
        """提交一个 TTS 任务（立即启动，不阻塞）。"""
        index = self._next_index
        self._next_index += 1
        self._total_submitted += 1
        task = asyncio.create_task(self._run_tts(index, text))
        self._tasks[index] = task
        log.info("[OrderedTtsEmitter] Submitted task index=%d, text=%s",
                 index, text[:30])
        if index > 0:
            asyncio.create_task(self._try_send())
        return task

    async def _run_tts(self, index: int, text: str) -> None:
        """
        执行单个 TTS 任务。

        直接复用 _SynthSession.done_event（TTS 服务内部已通过 call_soon_threadsafe
        正确跨线程设置），不再创建独立的 asyncio.Event，避免双重等待导致的超时。
        """
        async with self._semaphore:
            chunks: list[bytes] = []
            err: list[Exception] = []

            def on_audio(chunk: bytes) -> None:
                chunks.append(chunk)

            def on_error(e: Exception) -> None:
                err.append(e)

            try:
                await self._tts_service.concurrent_synthesize_stream(
                    session_id=self._session_id,
                    text=text,
                    on_audio=on_audio,
                    on_complete=None,  # done_event 已包含完成信号
                    on_error=on_error,
                )
            except Exception as e:
                err.append(e)

            result = _TtsTask(
                index=index,
                text=text,
                chunks=chunks,
                error=err[0] if err else None,
            )

            async with self._lock:
                self._results[index] = result
                log.info("[OrderedTtsEmitter] Task %d complete: chunks=%d, error=%s",
                         index, len(chunks), result.error)

            await self._try_send()

    async def _try_send(self) -> None:
        """尝试发送已完成的 TTS 结果（按序，带发送间隔控制）。"""
        async with self._lock:
            while self._sent_index in self._results:
                # LLM 尚未结束时保留当前最后一句，等下一句到达或 drain() 后再确定 isLast。
                if not self._finished and self._sent_index == self._total_submitted - 1:
                    break
                result = self._results.pop(self._sent_index)
                if result.error:
                    log.warning("[OrderedTtsEmitter] TTS error for index=%d: %s",
                               result.index, result.error)
                elif result.chunks:
                    is_last = (
                        self._finished
                        and self._sent_index == self._total_submitted - 1
                    )
                    log.info(
                        "[OrderedTtsEmitter] Sending index=%d, source_chunks=%d, is_last=%s",
                        result.index,
                        len(result.chunks),
                        is_last,
                    )
                    await self._throttle_send()
                    await self._send_fn_async(
                        b"".join(result.chunks), self._sent_index, is_last
                    )
                self._sent_index += 1

    async def _throttle_send(self) -> None:
        """发送节流：确保相邻 chunk 发送间隔不小于 CHUNK_SEND_INTERVAL_MS。"""
        now_ns = time.perf_counter_ns()
        elapsed_ms = (now_ns - self._last_send_time) / 1_000_000
        if elapsed_ms < CHUNK_SEND_INTERVAL_MS:
            await asyncio.sleep((CHUNK_SEND_INTERVAL_MS - elapsed_ms) / 1000)
        self._last_send_time = time.perf_counter_ns()

    async def drain(self) -> int:
        """等待所有 TTS 任务完成并发送。"""
        self._finished = True

        # 等待所有任务完成
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:
            concurrent_batches = (
                len(pending) + self._max_concurrent - 1
            ) // self._max_concurrent
            drain_timeout = self._timeout * concurrent_batches + 2
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=drain_timeout,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "[OrderedTtsEmitter] Drain timeout after %.1fs, pending=%d, batches=%d",
                    drain_timeout,
                    len(pending),
                    concurrent_batches,
                )
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        # 发送剩余结果
        await self._try_send()

        return self._sent_index

    def shutdown(self) -> None:
        """清理资源。"""
        self._finished = True
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
