"""
DashScope Voice Client - Using official DashScope SDK.

This module provides ASR and TTS services using the official DashScope SDK.
The SDK uses WebSocket-based communication with callback handlers.

Reference:
- ASR: https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-python-sdk
- TTS: https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.config.settings import get_settings


log = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class RealtimeAsrCallbacks:
    """Callbacks for real-time ASR."""
    on_final: Callable[[str], None] = field(default=lambda x: None)
    on_partial: Callable[[str], None] = field(default=lambda x: None)
    on_ready: Callable[[], None] = field(default=lambda x: None)
    on_error: Callable[[Exception], None] = field(default=lambda x: None)


@dataclass
class RealtimeTtsCallbacks:
    """Callbacks for real-time TTS."""
    on_audio: Callable[[bytes], None] = field(default=lambda x: None)
    on_complete: Callable[[], None] = field(default=lambda x: None)
    on_error: Callable[[Exception], None] = field(default=lambda x: None)


def _get_api_key() -> str:
    """Get API key from settings or environment."""
    from app.config.settings import get_settings
    settings = get_settings()
    api_key = settings.ai.bailian_api_key
    if not api_key:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        api_key = os.environ.get("AI_BAILIAN_API_KEY", "")
    return api_key


def _set_dashscope_api_key() -> None:
    """Set dashscope global API key if not already set."""
    try:
        import dashscope
        if not getattr(dashscope, "api_key", None):
            api_key = _get_api_key()
            if api_key:
                dashscope.api_key = api_key
                log.debug("DashScope API key configured")
    except ImportError:
        pass


class RealtimeAsrSession:
    """
    Real-time ASR session using DashScope OmniRealtime API.

    This implements speech recognition with server-side VAD
    (Voice Activity Detection) for automatic sentence detection.
    """

    def __init__(
        self,
        session_id: str,
        callbacks: RealtimeAsrCallbacks,
    ) -> None:
        self._session_id = session_id
        self._callbacks = callbacks
        self._cfg = settings.voice_interview
        self._running = False
        self._ready = False
        self._ready_event = threading.Event()
        self._conversation = None

    @property
    def is_ready(self) -> bool:
        return self._ready

    def start(self) -> None:
        """Start the ASR session in a background thread."""
        if self._running:
            return

        self._running = True
        thread = threading.Thread(target=self._connect_and_run, daemon=True)
        thread.start()

    def send_audio(self, audio_data: bytes) -> None:
        """Send audio data to the ASR service."""
        if not self._ready or not self._running:
            raise RuntimeError(f"ASR session not ready: {self._session_id}")

        if self._conversation:
            audio_b64 = base64.b64encode(audio_data).decode("ascii")
            try:
                self._conversation.append_audio(audio_b64)
            except Exception as e:
                log.error("[ASR Session %s] Send error: %s", self._session_id, e)
                raise RuntimeError(f"ASR append failed: {e}")

    def stop(self) -> None:
        """Stop the ASR session."""
        self._running = False
        self._ready = False
        if self._conversation:
            try:
                self._conversation.end_session()
                self._conversation.close()
            except Exception:
                pass
            self._conversation = None

    def _connect_and_run(self) -> None:
        """Connect to ASR WebSocket and run the session."""
        try:
            from dashscope.audio.qwen_omni import (
                OmniRealtimeConversation,
                OmniRealtimeCallback,
                MultiModality,
                AudioFormat,
            )
            from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams
        except ImportError as e:
            log.error("[ASR] Failed to import dashscope SDK: %s", e)
            if self._callbacks.on_error:
                self._callbacks.on_error(RuntimeError(f"dashscope SDK not installed: {e}"))
            self._running = False
            return

        try:
            # Set API key globally for this thread
            import dashscope
            api_key = _get_api_key()
            if not api_key:
                log.error("[ASR] No API key configured")
                if self._callbacks.on_error:
                    self._callbacks.on_error(RuntimeError("No API key configured for ASR"))
                self._running = False
                return
            # Set globally for the SDK
            dashscope.api_key = api_key

            log.info("[ASR Session %s] Connecting with model: %s, url: %s",
                     self._session_id, self._cfg.asr_model, self._cfg.asr_url)

            # Create conversation with callback
            callback = _AsrCallbackHandler(self._session_id, self._callbacks)

            self._conversation = OmniRealtimeConversation(
                model=self._cfg.asr_model,
                callback=callback,
                url=self._cfg.asr_url,
                api_key=api_key,
            )

            # Connect
            self._conversation.connect()

            # Configure transcription using direct parameters
            self._conversation.update_session(
                output_modalities=[MultiModality.TEXT],
                enable_input_audio_transcription=True,
                input_audio_transcription_model=self._cfg.asr_model,
                enable_turn_detection=self._cfg.asr_enable_turn_detection,
                turn_detection_type=self._cfg.asr_turn_detection_type or "server_vad",
                turn_detection_threshold=self._cfg.asr_turn_detection_threshold or 0.2,
                turn_detection_silence_duration_ms=self._cfg.asr_turn_detection_silence_duration_ms or 800,
                transcription_params=TranscriptionParams(
                    language=self._cfg.asr_language or "zh",
                    sample_rate=self._cfg.asr_sample_rate or 16000,
                    input_audio_format=self._cfg.asr_format or "pcm",
                ),
            )

            # Mark as ready
            self._ready = True
            self._ready_event.set()
            if self._callbacks.on_ready:
                self._callbacks.on_ready()

            log.info("[ASR Session %s] Started successfully with model: %s", self._session_id, self._cfg.asr_model)

            # Keep the thread alive
            while self._running:
                time.sleep(0.1)

        except Exception as e:
            log.error("[ASR Session %s] Connection error: %s", self._session_id, e)
            if self._callbacks.on_error:
                self._callbacks.on_error(e)
            self._running = False
            self._ready = False

    def await_ready(self, timeout_ms: int = 5000) -> bool:
        """Wait for session to be ready."""
        return self._ready_event.wait(timeout=timeout_ms / 1000)


class _AsrCallbackHandler:
    """Handler for ASR callback events. Inherits from OmniRealtimeCallback."""

    def __init__(self, session_id: str, callbacks: RealtimeAsrCallbacks):
        from dashscope.audio.qwen_omni import OmniRealtimeCallback
        self._session_id = session_id
        self._callbacks = callbacks
        self._ready = False

    def on_open(self) -> None:
        log.debug("[ASR Session %s] WebSocket opened", self._session_id)

    def on_close(self, close_status_code, close_msg) -> None:
        log.debug("[ASR Session %s] WebSocket closed: %s - %s", self._session_id, close_status_code, close_msg)

    def on_event(self, message: str) -> None:
        """Handle server events. Message is a JSON string."""
        try:
            # Parse JSON string if needed
            if isinstance(message, str):
                msg = json.loads(message)
            else:
                msg = message

            msg_type = msg.get("type", "")

            if msg_type == "session.created":
                log.debug("[ASR Session %s] Session created", self._session_id)
                self._ready = True

            elif msg_type == "session.updated":
                log.debug("[ASR Session %s] Session updated", self._session_id)

            elif msg_type in ("conversation.item.input_audio_transcription.completed",
                              "conversation.item.input_audio_transcription.text"):
                # Final transcription
                transcript = self._extract_transcript(msg)
                if transcript:
                    log.debug("[ASR Session %s] Final transcript: %s", self._session_id, transcript)
                    if self._callbacks.on_final:
                        self._callbacks.on_final(transcript)

            elif msg_type == "error":
                error = msg.get("error", {})
                error_msg = f"{error.get('type', 'unknown')}/{error.get('code', 'unknown')}: {error.get('message', 'Unknown')}"
                log.error("[ASR Session %s] Error: %s", self._session_id, error_msg)
                if self._callbacks.on_error:
                    self._callbacks.on_error(RuntimeError(error_msg))

        except Exception as e:
            log.error("[ASR Session %s] Error handling event: %s", self._session_id, e)

    def _extract_transcript(self, msg: dict) -> str:
        """Extract transcript from message."""
        # Check for direct transcript
        if "transcript" in msg:
            return str(msg["transcript"])
        # Check for text/stash format
        if "text" in msg:
            return str(msg["text"])
        return ""


class RealtimeTtsSession:
    """
    Real-time TTS session using DashScope QwenTtsRealtime API.

    This implements text-to-speech synthesis with streaming audio output.
    """

    def __init__(
        self,
        callbacks: RealtimeTtsCallbacks,
    ) -> None:
        self._callbacks = callbacks
        self._cfg = settings.voice_interview
        self._running = False
        self._audio_chunks: list[bytes] = []
        self._complete_event = threading.Event()
        self._tts = None

    def synthesize(self, text: str, timeout: float = 30) -> bytes:
        """
        Synchronously synthesize text to speech and return PCM audio bytes.

        Args:
            text: Text to synthesize
            timeout: Timeout in seconds

        Returns:
            PCM audio bytes, or empty bytes if synthesis failed
        """
        if not text.strip():
            return b""

        self._audio_chunks = []
        self._complete_event.clear()
        self._running = True

        try:
            from dashscope.audio.qwen_tts_realtime import (
                QwenTtsRealtime,
                QwenTtsRealtimeCallback,
                AudioFormat,
            )
        except ImportError as e:
            log.error("[TTS] Failed to import dashscope SDK: %s", e)
            if self._callbacks.on_error:
                self._callbacks.on_error(RuntimeError(f"dashscope SDK not installed: {e}"))
            return b""

        try:
            # Set API key globally for this thread (SDK uses dashscope.api_key)
            import dashscope
            api_key = _get_api_key()
            if not api_key:
                log.error("[TTS] No API key configured")
                if self._callbacks.on_error:
                    self._callbacks.on_error(RuntimeError("No API key configured for TTS"))
                return b""
            dashscope.api_key = api_key

            log.info("[TTS] Starting synthesis with model: %s, voice: %s, url: %s",
                     self._cfg.tts_model, self._cfg.tts_voice, self._cfg.tts_url)

            # Create callback handler
            callback = _TtsCallbackHandler(self._callbacks, self._audio_chunks, self._complete_event)

            # Create TTS instance (SDK uses dashscope.api_key internally)
            self._tts = QwenTtsRealtime(
                model=self._cfg.tts_model,
                callback=callback,
                url=self._cfg.tts_url,
            )

            # Connect
            self._tts.connect()

            # Configure session using direct parameters (not builder pattern)
            self._tts.update_session(
                voice=self._cfg.tts_voice,
                response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                mode=self._cfg.tts_mode or "server_commit",
                language_type=self._cfg.tts_language_type or "auto",
                speech_rate=self._cfg.tts_speech_rate,
                volume=self._cfg.tts_volume,
            )

            # Send text and commit
            self._tts.append_text(text)
            self._tts.commit()

            log.info("[TTS] Text sent for synthesis, length: %d", len(text))

            # Wait for completion
            completed = self._complete_event.wait(timeout=timeout)
            if not completed:
                log.warning("[TTS] Synthesis timed out after %s seconds", timeout)
                return b""

        except Exception as e:
            log.error("[TTS] Synthesis error: %s", e)
            if self._callbacks.on_error:
                self._callbacks.on_error(e)
            return b""
        finally:
            self._running = False
            if self._tts:
                try:
                    self._tts.close()
                except Exception:
                    pass

        return b"".join(self._audio_chunks)


class _TtsCallbackHandler:
    """Handler for TTS callback events. Inherits from QwenTtsRealtimeCallback."""

    def __init__(self, callbacks: RealtimeTtsCallbacks, audio_chunks: list, complete_event: threading.Event):
        self._callbacks = callbacks
        self._audio_chunks = audio_chunks
        self._complete_event = complete_event

    def on_open(self) -> None:
        log.debug("[TTS] WebSocket opened")

    def on_close(self, close_status_code, close_msg) -> None:
        log.debug("[TTS] WebSocket closed: %s - %s", close_status_code, close_msg)
        # Don't set complete_event here - it should be set by response.done

    def on_event(self, message: str) -> None:
        """Handle server events. Message is a JSON string."""
        try:
            # Parse JSON string if needed
            if isinstance(message, str):
                msg = json.loads(message)
            else:
                msg = message

            msg_type = msg.get("type", "")

            if msg_type == "session.created":
                log.debug("[TTS] Session created")

            elif msg_type == "session.updated":
                log.debug("[TTS] Session updated")

            elif msg_type == "response.audio.delta":
                # Audio chunk received
                delta = msg.get("delta", "")
                if delta:
                    try:
                        audio_chunk = base64.b64decode(delta)
                        self._audio_chunks.append(audio_chunk)
                        if self._callbacks.on_audio:
                            self._callbacks.on_audio(audio_chunk)
                    except Exception as e:
                        log.error("[TTS] Failed to decode audio delta: %s", e)

            elif msg_type == "response.done":
                log.debug("[TTS] Response done")
                self._complete_event.set()
                if self._callbacks.on_complete:
                    self._callbacks.on_complete()

            elif msg_type == "error":
                error = msg.get("error", {})
                error_msg = f"{error.get('type', 'unknown')}/{error.get('code', 'unknown')}: {error.get('message', 'Unknown')}"
                log.error("[TTS] Error: %s", error_msg)
                self._complete_event.set()
                if self._callbacks.on_error:
                    self._callbacks.on_error(RuntimeError(error_msg))

        except Exception as e:
            log.error("[TTS] Error handling event: %s", e)
            self._complete_event.set()
            if self._callbacks.on_error:
                self._callbacks.on_error(e)


def synthesize_speech(text: str, model: str | None = None, voice: str | None = None) -> str:
    """
    Synthesize speech from text using DashScope real-time TTS.

    Returns base64 encoded PCM audio data.

    Args:
        text: Text to synthesize
        model: TTS model (defaults to qwen3-tts-flash-realtime)
        voice: Voice name (defaults to Cherry)

    Returns:
        Base64 encoded PCM audio data, or empty string on failure
    """
    log.info("[TTS] synthesize_speech called for %d characters", len(text))

    if not text.strip():
        return ""

    try:
        callbacks = RealtimeTtsCallbacks()
        session = RealtimeTtsSession(callbacks=callbacks)
        audio_bytes = session.synthesize(text, timeout=30)

        if audio_bytes:
            return base64.b64encode(audio_bytes).decode("ascii")
        return ""

    except Exception as e:
        log.error("[TTS] Synthesis failed: %s", e)
        return ""


def transcribe_audio(audio_b64: str, model: str | None = None, language: str | None = None) -> str:
    """
    Transcribe audio to text using DashScope Recognition ASR API.

    Args:
        audio_b64: Base64 encoded audio data (PCM 16kHz)
        model: ASR model (defaults to paraformer-realtime-v2)
        language: Language code (defaults to zh)

    Returns:
        Transcribed text, or empty string on failure
    """
    import dashscope
    from dashscope.audio.asr import Recognition, RecognitionCallback

    try:
        api_key = _get_api_key()
        if not api_key:
            log.error("[ASR] No API key configured")
            return ""
        dashscope.api_key = api_key

        # ASR 模型配置
        asr_model = "paraformer-realtime-v2"  # 使用专门的 ASR 模型
        format_audio = "pcm"
        sample_rate = 16000

        # 创建事件用于同步
        result_event = threading.Event()
        result_holder = [""]
        error_holder = [None]
        is_complete = [False]

        class TranscribeCallback(RecognitionCallback):
            def on_open(self) -> None:
                log.debug("[ASR] Connection opened")

            def on_complete(self) -> None:
                log.debug("[ASR] Recognition complete")
                is_complete[0] = True
                result_event.set()

            def on_error(self, result) -> None:
                error_msg = f"ASR error: code={result.code}, message={result.message}"
                log.error("[ASR] %s", error_msg)
                error_holder[0] = RuntimeError(error_msg)
                result_event.set()

            def on_close(self) -> None:
                log.debug("[ASR] Connection closed")

            def on_event(self, result) -> None:
                nonlocal result_holder
                try:
                    if hasattr(result, 'output') and result.output:
                        sentence = result.output.get("sentence", {})
                        if isinstance(sentence, dict):
                            text = sentence.get("text", "")
                            if text:
                                result_holder[0] = text
                                log.info("[ASR] Transcription: %s", text)
                        elif isinstance(sentence, list) and len(sentence) > 0:
                            # 多个句子的情况，取最后一个
                            text = sentence[-1].get("text", "") if isinstance(sentence[-1], dict) else str(sentence[-1])
                            if text:
                                result_holder[0] = text
                                log.info("[ASR] Transcription: %s", text)
                except Exception as e:
                    log.error("[ASR] Error processing result: %s", e)

        # 解码音频数据
        audio_bytes = base64.b64decode(audio_b64)
        if len(audio_bytes) < 3200:  # 少于 200ms 的音频
            log.warning("[ASR] Audio data too short: %d bytes", len(audio_bytes))
            # 即使音频很短也尝试识别
            if len(audio_bytes) == 0:
                return ""

        # 创建回调和识别器
        callback = TranscribeCallback()
        recognizer = Recognition(
            model=asr_model,
            callback=callback,
            format=format_audio,
            sample_rate=sample_rate,
        )

        # 启动识别
        recognizer.start()

        # 发送音频数据（分块发送）
        chunk_size = 12800  # 约 800ms 的音频
        offset = 0
        while offset < len(audio_bytes):
            chunk = audio_bytes[offset:offset + chunk_size]
            if len(chunk) > 0:
                recognizer.send_audio_frame(chunk)
            offset += chunk_size
            # 添加小延迟避免发送过快
            time.sleep(0.05)

        # 结束识别
        recognizer.stop()

        # 等待结果（超时 10 秒）
        result_event.wait(timeout=10)

        # 清理
        try:
            recognizer.__del__()
        except Exception:
            pass

        if error_holder[0]:
            log.warning("[ASR] Transcription error: %s", error_holder[0])
            return ""

        return result_holder[0]

    except Exception as e:
        log.warning("[ASR] Transcription failed: %s", e)
        import traceback
        log.warning("[ASR] Traceback: %s", traceback.format_exc())
        return ""
