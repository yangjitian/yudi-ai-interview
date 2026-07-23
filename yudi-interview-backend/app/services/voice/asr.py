"""
阿里云实时 ASR 服务 - 使用新版 API

参考 Java 版本架构：
- 维持持续的 WebSocket 连接
- 持续接收音频流并实时返回识别结果
- 支持 partial 和 final 两种结果回调

API 参考: https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-interaction-process
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import uuid
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config.settings import get_settings


log = logging.getLogger(__name__)
settings = get_settings()
NOISE_TRANSCRIPTS = {"啊", "啊啊", "嗯", "嗯嗯", "呃", "额", "哦", "喂"}
PUNCTUATION_CHARS = "。.!！?？，,、;；:：~… "


class RealtimeAsrSession:
    """
    实时 ASR 会话 - 维持 WebSocket 长连接

    使用阿里云 qwen3-asr-flash-realtime 模型的新版 API
    """

    def __init__(
        self,
        session_id: str,
        on_partial: Callable[[str], None] | None = None,
        on_final: Callable[[str], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._session_id = session_id
        self._on_partial = on_partial or (lambda x: None)
        self._on_final = on_final or (lambda x: None)
        self._on_ready = on_ready or (lambda: None)
        self._on_error = on_error or (lambda x: None)

        self._cfg = settings.voice_interview
        self._running = False
        self._ready = False
        self._ready_event = threading.Event()
        self._ws = None

        # 持久的事件循环和线程
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_ready(self) -> bool:
        return self._ready

    def start(self) -> None:
        """在独立线程中启动 ASR 会话"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def await_ready(self, timeout_ms: int = 10000) -> bool:
        """等待会话就绪"""
        return self._ready_event.wait(timeout=timeout_ms / 1000)

    def send_audio(self, audio_data: bytes) -> None:
        """
        发送音频数据到 ASR 服务

        音频数据应该是 PCM 16kHz 单声道 Int16 格式
        """
        if not self._ready or not self._running or not self._loop:
            log.warning("[ASR %s] Session not ready, dropping audio", self._session_id)
            return

        # 在事件循环中调度异步发送
        try:
            asyncio.run_coroutine_threadsafe(
                self._send_audio_frame(audio_data),
                self._loop
            )
        except Exception as e:
            log.error("[ASR %s] Send audio error: %s", self._session_id, e)

    def stop(self) -> None:
        """停止 ASR 会话"""
        self._running = False
        self._ready = False

        if self._loop and self._thread and self._thread.is_alive():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._close_connection(),
                    self._loop
                )
            except Exception:
                pass

    def _run_loop(self) -> None:
        """在独立线程中运行事件循环"""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._connect_and_run())
        except Exception as e:
            log.error("[ASR %s] Event loop error: %s", self._session_id, e)
            self._on_error(e)
        finally:
            if self._loop:
                self._loop.close()
                self._loop = None

    async def _connect_and_run(self) -> None:
        """连接 ASR 并运行"""
        try:
            from websockets.asyncio.client import connect

            # 获取 API key
            api_key = self._get_api_key()
            if not api_key:
                log.error("[ASR %s] No API key configured", self._session_id)
                self._on_error(RuntimeError("No API key configured"))
                self._running = False
                return

            model = self._cfg.asr_model or "qwen3-asr-flash-realtime"
            url = self._build_url(self._cfg.asr_url, model)
            log.info("[ASR %s] Connecting to %s", self._session_id, url)

            # 建立 WebSocket 连接
            self._ws = await connect(
                url,
                additional_headers={"Authorization": f"Bearer {api_key}"}
            )
            log.info("[ASR %s] WebSocket connected", self._session_id)

            # 发送会话配置
            await self._send_session_config()

            # 标记就绪
            self._ready = True
            self._ready_event.set()
            self._on_ready()

            log.info("[ASR %s] Session started successfully", self._session_id)

            # 接收消息循环
            while self._running:
                try:
                    message = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
                    await self._handle_message(message)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    if self._running:
                        log.error("[ASR %s] Receive error: %s", self._session_id, e)
                        self._on_error(e)
                    break

        except ImportError:
            log.error("[ASR %s] websockets not installed", self._session_id)
            self._on_error(RuntimeError("websockets not installed"))
            self._running = False

        except Exception as e:
            log.error("[ASR %s] Error: %s", self._session_id, e)
            self._on_error(e)
            self._running = False
            self._ready = False

    @staticmethod
    def _build_url(base_url: str, model: str) -> str:
        parts = urlsplit(base_url)
        query = dict(parse_qsl(parts.query))
        query["model"] = model
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        ))

    async def _send_session_config(self) -> None:
        """发送会话配置"""
        config = {
            "event_id": f"event_{uuid.uuid4().hex[:8]}",
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": "pcm",
                "sample_rate": self._cfg.asr_sample_rate or 16000,
                "input_audio_transcription": {
                    "language": "zh"
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.0,
                    "silence_duration_ms": self._cfg.asr_turn_detection_silence_duration_ms or 800
                }
            }
        }
        await self._ws.send(json.dumps(config))
        log.debug("[ASR %s] Session config sent", self._session_id)

    async def _send_audio_frame(self, audio_data: bytes) -> None:
        """发送音频帧"""
        if not self._ws:
            return

        frame = {
            "event_id": f"event_{uuid.uuid4().hex[:8]}",
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(audio_data).decode("ascii")
        }
        await self._ws.send(json.dumps(frame))

    async def _commit_audio_buffer(self) -> None:
        """提交音频缓冲区（触发识别）"""
        if not self._ws:
            return

        frame = {
            "event_id": f"event_{uuid.uuid4().hex[:8]}",
            "type": "input_audio_buffer.commit"
        }
        await self._ws.send(json.dumps(frame))

    async def _handle_message(self, message: str | bytes) -> None:
        """处理收到的消息"""
        if isinstance(message, bytes):
            message = message.decode('utf-8')

        try:
            data = json.loads(message)
            msg_type = data.get("type", "")

            if msg_type == "session.created":
                log.debug("[ASR %s] Session created", self._session_id)

            elif msg_type == "session.updated":
                log.debug("[ASR %s] Session updated", self._session_id)

            elif msg_type == "conversation.item.input_audio_transcription.completed":
                # 完整的转写结果
                text = data.get("transcript", "")
                if self._is_meaningful_transcript(text):
                    log.info("[ASR %s] Final transcription: %s", self._session_id, text)
                    self._on_final(text)

            elif msg_type == "conversation.item.input_audio_transcription.in_progress":
                # 部分转写结果（实时字幕）
                text = data.get("transcript", "")
                if self._is_meaningful_transcript(text):
                    log.debug("[ASR %s] Partial transcription: %s", self._session_id, text)
                    self._on_partial(text)

            elif msg_type == "conversation.item.created":
                # 新的对话项创建
                pass

            elif msg_type == "response.done":
                # 响应完成
                pass

            elif msg_type == "error":
                error_msg = data.get("error", {}).get("message", "Unknown error")
                log.error("[ASR %s] API error: %s", self._session_id, error_msg)
                self._on_error(RuntimeError(error_msg))

        except json.JSONDecodeError:
            log.warning("[ASR %s] Invalid JSON: %s", self._session_id, message[:100])
        except Exception as e:
            log.error("[ASR %s] Handle message error: %s", self._session_id, e)

    @staticmethod
    def _is_meaningful_transcript(text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized:
            return False
        content = normalized.strip(PUNCTUATION_CHARS)
        if not content:
            return False
        return content not in NOISE_TRANSCRIPTS

    async def _close_connection(self) -> None:
        """关闭连接"""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _get_api_key(self) -> str:
        """获取 API key"""
        api_key = settings.ai.bailian_api_key
        if api_key:
            return api_key
        import os
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if api_key:
            return api_key
        api_key = os.environ.get("AI_BAILIAN_API_KEY", "")
        return api_key


class AsrService:
    """
    ASR 服务 - 管理实时 ASR 会话
    """

    def __init__(self) -> None:
        self._cfg = settings.voice_interview
        self._sessions: dict[str, RealtimeAsrSession] = {}
        self._sessions_lock = threading.Lock()

    def create_session(
        self,
        on_partial: Callable[[str], None] | None = None,
        on_final: Callable[[str], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> RealtimeAsrSession:
        """创建新的 ASR 会话"""
        session_id = str(uuid.uuid4())[:8]

        session = RealtimeAsrSession(
            session_id=session_id,
            on_partial=on_partial,
            on_final=on_final,
            on_ready=on_ready,
            on_error=on_error,
        )
        session.start()

        with self._sessions_lock:
            self._sessions[session_id] = session

        log.info("[AsrService] Created session: %s", session_id)
        return session

    def get_session(self, session_id: str) -> RealtimeAsrSession | None:
        """获取会话"""
        with self._sessions_lock:
            return self._sessions.get(session_id)

    def remove_session(self, session_id: str) -> None:
        """移除会话"""
        with self._sessions_lock:
            session = self._sessions.pop(session_id, None)
            if session:
                session.stop()
                log.info("[AsrService] Removed session: %s", session_id)

    def is_ready(self, session_id: str) -> bool:
        """检查会话是否就绪"""
        with self._sessions_lock:
            session = self._sessions.get(session_id)
            return session is not None and session.is_ready
