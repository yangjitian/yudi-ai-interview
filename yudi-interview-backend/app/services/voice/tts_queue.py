"""
TTS 任务队列 - 简化版（复用 TtsService 实例）。

设计原则：
1. 直接复用 ws_handler 中的 TtsService 实例（已预热）
2. 串行处理任务，避免 DashScope TTS 并发问题
3. 同步回调 + 事件循环派发模式

作者：AI Assistant
日期：2026-06-18
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from app.config.settings import get_settings


log = logging.getLogger(__name__)
settings = get_settings()


class TtsTaskState(Enum):
    """TTS 任务状态。"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TtsTask:
    """单个 TTS 合成任务。"""
    task_id: str = field(default_factory=lambda: f"tts_{uuid.uuid4().hex[:8]}")
    text: str = ""
    index: int = 0
    state: TtsTaskState = TtsTaskState.PENDING
    on_audio: Callable[[bytes], None] | None = None
    on_complete: Callable[[], None] | None = None
    on_error: Callable[[Exception], None] | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    retry_count: int = 0
    max_retries: int = 3
    last_error: Exception | None = None

    def mark_processing(self) -> None:
        self.state = TtsTaskState.PROCESSING

    def mark_completed(self) -> None:
        if self.state == TtsTaskState.COMPLETED:
            return  # 避免重复标记
        self.state = TtsTaskState.COMPLETED
        self.done_event.set()
        log.info("[TtsTask] %s marked completed (text=%s)", self.task_id, self.text[:30])

    def mark_failed(self, error: Exception) -> None:
        self.state = TtsTaskState.FAILED
        self.last_error = error
        self.done_event.set()

    def mark_cancelled(self) -> None:
        self.state = TtsTaskState.CANCELLED
        self.done_event.set()

    def can_retry(self) -> bool:
        return self.retry_count < self.max_retries

    def increment_retry(self) -> None:
        self.retry_count += 1


class TtsQueue:
    """
    TTS 任务队列（生产者-消费者模式）。

    设计原则：
    1. 不创建独立的 TtsService，而是由外部注入
    2. 串行处理任务
    3. 使用 asyncio.Queue 的 task_done/join 机制确保队列清空后再停止
    """

    def __init__(self, tts_service, session_id: str, pool_size: int = 1) -> None:
        """
        初始化 TTS 队列。

        Args:
            tts_service: TtsService 实例（由 ws_handler 注入）
            session_id: WebSocket session ID（与 ws_handler 预热的一致）
            pool_size: 消费者数量（建议 1，串行处理）
        """
        self._tts_service = tts_service
        self._session_id = session_id  # 与 ws_handler 预热的一致
        self._pool_size = max(1, pool_size)
        self._task_queue: asyncio.Queue[TtsTask | None] = asyncio.Queue()
        self._running = False
        self._draining = False
        self._consumer_tasks: list[asyncio.Task] = []
        self._cancelled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # 队列同步：使用 Queue 的 unfinished_tasks 机制
        self._unfinished_tasks: int = 0
        self._all_tasks_done_event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        """启动消费者。"""
        # 清理已完成的旧任务
        if self._consumer_tasks:
            stale_tasks = [t for t in self._consumer_tasks if t.done()]
            for t in stale_tasks:
                self._consumer_tasks.remove(t)
            if stale_tasks:
                log.info("[TtsQueue] Cleared %d stale consumer tasks", len(stale_tasks))

        # 如果没有活跃的 Consumer，才启动新的
        if self._running and self._consumer_tasks and not all(t.done() for t in self._consumer_tasks):
            log.info("[TtsQueue] Already running (%d active consumers)", len(self._consumer_tasks))
            return

        self._running = True
        self._cancelled = False
        self._draining = False
        self._unfinished_tasks = 0
        self._loop = asyncio.get_running_loop()

        for i in range(self._pool_size):
            task = asyncio.create_task(self._consumer_loop(i))
            self._consumer_tasks.append(task)
        log.info("[TtsQueue] Started %d consumer(s) (total tasks=%d)", self._pool_size, len(self._consumer_tasks))

    async def stop(self, drain: bool = True) -> None:
        """
        停止消费者。

        Args:
            drain: 如果为 True，等待所有队列任务完成后再停止。
                   如果为 False，立即取消所有任务。
        """
        if drain:
            log.info("[TtsQueue] Stop requested (drain=True, qsize=%d, unfinished=%d)",
                     self._task_queue.qsize(), self._unfinished_tasks)
            # 发送 sentinel 标记
            self._draining = True
            try:
                self._task_queue.put_nowait(None)  # sentinel
                log.info("[TtsQueue] Drain sentinel sent")
            except Exception as e:
                log.warning("[TtsQueue] Failed to send sentinel: %s", e)

            # 等待所有任务完成（使用事件同步，而非超时取消）
            try:
                await asyncio.wait_for(self._all_tasks_done_event.wait(), timeout=30.0)
                log.info("[TtsQueue] All tasks completed, queue drained")
            except asyncio.TimeoutError:
                log.warning("[TtsQueue] Drain timeout (qsize=%d, unfinished=%d), forcing stop",
                           self._task_queue.qsize(), self._unfinished_tasks)

            # 取消消费者
            for task in self._consumer_tasks:
                if not task.done():
                    task.cancel()

            # 等待消费者完全停止
            if self._consumer_tasks:
                await asyncio.gather(*self._consumer_tasks, return_exceptions=True)

            self._running = False
            self._consumer_tasks.clear()
        else:
            log.info("[TtsQueue] Stop requested (drain=False, cancelling immediately)")
            self._running = False
            self._cancelled = True

            # 取消消费者
            for task in self._consumer_tasks:
                if not task.done():
                    task.cancel()

            if self._consumer_tasks:
                await asyncio.gather(*self._consumer_tasks, return_exceptions=True)
            self._consumer_tasks.clear()

            # 清空队列
            while not self._task_queue.empty():
                try:
                    item = self._task_queue.get_nowait()
                    if item is not None:
                        item.mark_cancelled()
                    self._task_queue.task_done()
                except asyncio.QueueEmpty:
                    break

        self._all_tasks_done_event.clear()
        log.info("[TtsQueue] Stopped (qsize=%d)", self._task_queue.qsize())

    async def enqueue(
        self,
        text: str,
        index: int,
        on_audio: Callable[[bytes], None],
        on_complete: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> TtsTask:
        """将 TTS 任务加入队列。如果 Consumer 已停止，自动重启。"""
        # 自动重启 Consumer（如果已停止）
        if not self._running or not self._consumer_tasks or all(t.done() for t in self._consumer_tasks):
            log.info("[TtsQueue] Consumer dead or not started, auto-restarting (running=%s, tasks=%s)",
                     self._running, [not t.done() for t in self._consumer_tasks] if self._consumer_tasks else [])
            await self.start()

        task = TtsTask(
            text=text,
            index=index,
            on_audio=on_audio,
            on_complete=on_complete,
            on_error=on_error,
            max_retries=settings.voice_interview.tts_max_retries or 3,
        )
        await self._task_queue.put(task)
        self._unfinished_tasks += 1  # 标记有新任务
        consumer_alive = [not t.done() for t in self._consumer_tasks] if self._consumer_tasks else []
        log.info(
            "[TtsQueue] Enqueued task %s (text=%s, index=%d, qsize=%d, unfinished=%d, running=%s, consumer_alive=%s)",
            task.task_id, text[:50], index, self._task_queue.qsize(), self._unfinished_tasks,
            self._running, consumer_alive,
        )
        return task

    async def _consumer_loop(self, consumer_id: int) -> None:
        """消费者循环。"""
        log.info("[TtsQueue] Consumer %d started (running=%s)", consumer_id, self._running)

        while self._running:
            task = None
            try:
                try:
                    task = await asyncio.wait_for(
                        self._task_queue.get(),
                        timeout=1.0,
                    )
                    log.info("[TtsQueue] Consumer %d got item from queue (qsize=%d, is_sentinel=%s, unfinished=%d)",
                             consumer_id, self._task_queue.qsize(), task is None, self._unfinished_tasks)
                except asyncio.TimeoutError:
                    continue

                # 处理 sentinel 标记（drain 信号）
                if task is None:
                    log.info("[TtsQueue] Consumer %d received sentinel, starting drain (qsize=%d, unfinished=%d)",
                             consumer_id, self._task_queue.qsize(), self._unfinished_tasks)
                    # 清空队列中剩余的普通任务
                    drained_count = 0
                    while True:
                        try:
                            next_task = self._task_queue.get_nowait()
                            log.info("[TtsQueue] Consumer %d drain: got task %s (qsize=%d, unfinished=%d)",
                                     consumer_id,
                                     getattr(next_task, 'task_id', 'unknown') if next_task else 'None',
                                     self._task_queue.qsize(), self._unfinished_tasks)
                            if next_task is None:
                                # 嵌套 sentinel，忽略
                                log.info("[TtsQueue] Consumer %d drain: ignoring nested sentinel", consumer_id)
                                continue
                            drained_count += 1
                            # 在 drain 模式下处理任务也需要正确更新 unfinished
                            task_was_pending = self._unfinished_tasks > 0
                            await self._process_task(next_task, consumer_id)
                            # 任务处理完成，更新计数器
                            if task_was_pending:
                                self._unfinished_tasks -= 1
                                try:
                                    self._task_queue.task_done()
                                except ValueError:
                                    pass
                            self._check_all_done()
                        except asyncio.QueueEmpty:
                            break
                    log.info("[TtsQueue] Consumer %d drained %d tasks, exiting (qsize=%d, unfinished=%d)",
                             consumer_id, drained_count, self._task_queue.qsize(), self._unfinished_tasks)
                    # sentinel 处理完毕，不再处理新任务，但需要等待正在处理的任务完成
                    # 这里不直接退出，而是设置 _drain_sentinel_received 标志
                    # 循环会因为 _running=False 在任务完成后退出
                    self._running = False
                    break

                if not self._running or self._cancelled:
                    log.info("[TtsQueue] Consumer %d cancelled (running=%s, cancelled=%s)",
                             consumer_id, self._running, self._cancelled)
                    task.mark_cancelled()
                    self._unfinished_tasks -= 1
                    self._task_queue.task_done()
                    self._check_all_done()
                    continue

                await self._process_task(task, consumer_id)
                self._unfinished_tasks -= 1
                self._task_queue.task_done()
                self._check_all_done()

            except asyncio.CancelledError:
                log.info("[TtsQueue] Consumer %d CancelledError (qsize=%d, unfinished=%d)",
                         consumer_id, self._task_queue.qsize(), self._unfinished_tasks)
                break
            except Exception as e:
                log.error("[TtsQueue] Consumer %d error: %s", consumer_id, e)
                if task is not None:
                    self._unfinished_tasks -= 1
                    try:
                        self._task_queue.task_done()
                    except ValueError:
                        pass  # task_done() called too many times

        log.info("[TtsQueue] Consumer %d stopped (qsize=%d, unfinished=%d)",
                 consumer_id, self._task_queue.qsize(), self._unfinished_tasks)

    def _check_all_done(self) -> None:
        """检查是否所有任务都已完成。"""
        if self._unfinished_tasks <= 0 and self._all_tasks_done_event.is_set() is False:
            log.info("[TtsQueue] All tasks done, signaling event")
            self._all_tasks_done_event.set()

    async def _process_task(self, task: TtsTask, consumer_id: int) -> None:
        """处理单个 TTS 任务。"""
        task.mark_processing()
        log.info(
            "[TtsQueue] Processing task %s (consumer=%d, text=%s, retry=%d)",
            task.task_id, consumer_id, task.text[:50], task.retry_count,
        )

        while True:
            try:
                await self._synthesize_with_tts(task)
                task.mark_completed()
                log.info("[TtsQueue] Task %s completed", task.task_id)
                return

            except Exception as e:
                task.last_error = e
                log.warning(
                    "[TtsQueue] Task %s failed (attempt %d/%d): %s",
                    task.task_id, task.retry_count + 1, task.max_retries, e,
                )

                if task.can_retry():
                    task.increment_retry()
                    delay = min(2 ** task.retry_count, 8)
                    log.info(
                        "[TtsQueue] Task %s retrying in %ds...",
                        task.task_id, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                task.mark_failed(e)
                try:
                    if task.on_error:
                        task.on_error(e)
                except Exception as callback_error:
                    log.warning(
                        "[TtsQueue] on_error callback failed for task %s: %s",
                        task.task_id, callback_error,
                    )
                return

    async def _synthesize_with_tts(self, task: TtsTask) -> None:
        """
        使用 TtsService 进行合成。

        关键：
        1. 使用统一的 session_id（与 ws_handler 相同），复用已有的连接池
        2. 同步回调 + asyncio.run_coroutine_threadsafe 派发到事件循环
        """
        # 使用 ws_handler 预热的 session_id，复用已有的连接池
        session_id = self._session_id
        loop = self._loop or asyncio.get_running_loop()

        local_pcm: list[bytes] = []
        pcm_lock = asyncio.Lock()
        completed = False

        async def _async_on_audio(chunk: bytes) -> None:
            """异步音频分片处理。"""
            nonlocal completed
            if completed:
                return
            async with pcm_lock:
                local_pcm.append(chunk)

        async def _async_on_complete() -> None:
            """异步完成处理。"""
            nonlocal completed
            if completed:
                return
            completed = True
            async with pcm_lock:
                if local_pcm:
                    pcm_concat = b"".join(local_pcm)
                    if pcm_concat and task.on_audio:
                        try:
                            task.on_audio(pcm_concat)
                        except Exception as e:
                            log.warning("[TtsQueue] on_audio callback error: %s", e)
            if task.on_complete:
                try:
                    task.on_complete()
                except Exception as e:
                    log.warning("[TtsQueue] on_complete callback error: %s", e)

        async def _async_on_error(e: Exception) -> None:
            """异步错误处理。"""
            log.warning("[TtsQueue] TTS synthesis error: %s", e)
            if task.on_error:
                try:
                    task.on_error(e)
                except Exception as callback_error:
                    log.warning("[TtsQueue] on_error callback error: %s", callback_error)

        def sync_on_audio(chunk: bytes) -> None:
            """同步回调：将异步处理派发到事件循环。"""
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_async_on_audio(chunk), loop)

        def sync_on_complete() -> None:
            """同步回调。"""
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_async_on_complete(), loop)

        def sync_on_error(e: Exception) -> None:
            """同步回调。"""
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_async_on_error(e), loop)

        try:
            await self._tts_service.synthesize_stream(
                session_id=session_id,
                text=task.text,
                on_audio=sync_on_audio,
                on_complete=sync_on_complete,
                on_error=sync_on_error,
            )
        except Exception as e:
            log.error("[TtsQueue] synthesize_stream failed: %s", e)
            sync_on_error(e)
            raise

        # 等待任务完成
        timeout = settings.voice_interview.tts_timeout_seconds or 30
        try:
            await asyncio.wait_for(task.done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("[TtsQueue] Task %s timeout after %ss", task.task_id, timeout)


__all__ = ["TtsQueue", "TtsTask", "TtsTaskState"]
