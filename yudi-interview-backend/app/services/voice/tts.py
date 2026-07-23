"""
DashScope TTS Service - Real-time WebSocket-based text-to-speech synthesis.

Uses the dashscope-realtime async SDK for high-quality speech synthesis.

Reference: https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import struct
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable

from app.config.settings import get_settings


log = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class TtsCallbacks:
    """Callbacks for TTS synthesis."""
    on_audio: Callable[[bytes], None] = field(default=lambda x: None)
    on_complete: Callable[[], None] = field(default=lambda x: None)
    on_error: Callable[[Exception], None] = field(default=lambda x: None)


class TtsSession:
    """
    A single TTS synthesis session.

    Manages a WebSocket connection for streaming audio synthesis.
    """

    def __init__(self, session_id: str, callbacks: TtsCallbacks) -> None:
        self._session_id = session_id
        self._callbacks = callbacks
        self._audio_chunks: list[bytes] = []
        self._complete = False
        self._tts_client = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def audio_chunks(self) -> list[bytes]:
        return self._audio_chunks

    @property
    def is_complete(self) -> bool:
        return self._complete

    def set_tts_client(self, client) -> None:
        """Set the TTS client."""
        self._tts_client = client

    def add_audio_chunk(self, chunk: bytes) -> None:
        """Add an audio chunk."""
        self._audio_chunks.append(chunk)
        if self._callbacks.on_audio:
            self._callbacks.on_audio(chunk)

    def complete(self) -> None:
        """Mark synthesis as complete."""
        self._complete = True
        if self._callbacks.on_complete:
            self._callbacks.on_complete()

    def get_audio(self) -> bytes:
        """Get the complete audio data."""
        return b"".join(self._audio_chunks)


class TtsService:
    """
    TTS (Text-to-Speech) Service.

    Provides speech synthesis using DashScope real-time TTS API with async support.

    Features:
    - Real-time synthesis with streaming audio
    - Configurable voice, speech rate, and volume
    - PCM to WAV conversion

    Reference:
        - https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide
        - https://github.com/mikuh/dashscope-realtime
    """

    def __init__(self) -> None:
        self._cfg = settings.voice_interview
        self._api_key = settings.ai.bailian_api_key

    async def synthesize(self, text: str) -> str:
        """
        Synthesize text to speech and return WAV audio as base64.

        Args:
            text: Text to synthesize

        Returns:
            Base64 encoded WAV audio data, or empty string on failure
        """
        if not text or not text.strip():
            log.debug("Empty text provided to TTS, returning empty result")
            return ""

        try:
            from app.services.voice.dashscope_client import RealtimeTtsSession, RealtimeTtsCallbacks

            # Create callbacks to collect audio
            audio_chunks = []
            complete_event = threading.Event()
            error_holder = [None]

            def on_audio(chunk: bytes) -> None:
                audio_chunks.append(chunk)

            def on_complete() -> None:
                complete_event.set()

            def on_error(e: Exception) -> None:
                error_holder[0] = e
                complete_event.set()

            callbacks = RealtimeTtsCallbacks(
                on_audio=on_audio,
                on_complete=on_complete,
                on_error=on_error,
            )

            # Create and run session
            session = RealtimeTtsSession(callbacks=callbacks)
            pcm_bytes = session.synthesize(text, timeout=30)

            if not pcm_bytes:
                log.warning("[TTS] Synthesis returned empty result")
                return ""

            # Convert PCM to WAV
            wav_base64 = self.pcm_to_wav_base64(pcm_bytes, self._cfg.tts_sample_rate)
            log.info("[TTS] Synthesized %d characters to %d bytes of audio",
                     len(text), len(pcm_bytes))
            return wav_base64

        except ImportError as e:
            log.error("[TTS] dashscope-realtime not installed: %s", e)
            return ""
        except Exception as e:
            log.error("[TTS] Synthesis error: %s", e)
            return ""

    async def synthesize_to_pcm(self, text: str) -> bytes:
        """
        Synthesize text to PCM audio bytes (without WAV header).

        Args:
            text: Text to synthesize

        Returns:
            Raw PCM audio bytes, or empty bytes on failure
        """
        if not text or not text.strip():
            log.debug("Empty text provided to TTS, returning empty result")
            return b""

        try:
            from app.services.voice.dashscope_client import RealtimeTtsSession, RealtimeTtsCallbacks

            audio_chunks = []
            complete_event = threading.Event()
            error_holder = [None]

            def on_audio(chunk: bytes) -> None:
                audio_chunks.append(chunk)

            def on_complete() -> None:
                complete_event.set()

            def on_error(e: Exception) -> None:
                error_holder[0] = e
                complete_event.set()

            callbacks = RealtimeTtsCallbacks(
                on_audio=on_audio,
                on_complete=on_complete,
                on_error=on_error,
            )

            session = RealtimeTtsSession(callbacks=callbacks)
            return session.synthesize(text, timeout=30)

        except Exception as e:
            log.error("[TTS] Synthesis error: %s", e)
            return b""

    def synthesize_sync(self, text: str) -> str:
        """
        Synchronously synthesize text to speech and return WAV audio as base64.

        This is a synchronous wrapper for use in non-async contexts.

        Args:
            text: Text to synthesize

        Returns:
            Base64 encoded WAV audio data, or empty string on failure
        """
        if not text or not text.strip():
            log.debug("Empty text provided to TTS, returning empty result")
            return ""

        try:
            from app.services.voice.dashscope_client import RealtimeTtsSession, RealtimeTtsCallbacks

            callbacks = RealtimeTtsCallbacks()
            session = RealtimeTtsSession(callbacks=callbacks)
            pcm_bytes = session.synthesize(text, timeout=30)

            if not pcm_bytes:
                log.warning("[TTS] Synthesis returned empty result")
                return ""

            wav_base64 = self.pcm_to_wav_base64(pcm_bytes, self._cfg.tts_sample_rate)
            log.info("[TTS] Synthesized %d characters to %d bytes of audio",
                     len(text), len(pcm_bytes))
            return wav_base64

        except ImportError as e:
            log.error("[TTS] dashscope-realtime not installed: %s", e)
            return ""
        except Exception as e:
            log.error("[TTS] Synthesis error: %s", e)
            return ""

    def pcm_to_wav_base64(self, pcm_bytes: bytes, sample_rate: int = 24000) -> str:
        """
        Convert PCM audio data to WAV format and return as base64.

        Args:
            pcm_bytes: Raw PCM audio data
            sample_rate: Audio sample rate (default 24000 for Qwen TTS)

        Returns:
            Base64 encoded WAV audio data
        """
        channels = 1
        bits_per_sample = 16
        byte_rate = sample_rate * channels * bits_per_sample // 8
        block_align = channels * bits_per_sample // 8
        data_size = len(pcm_bytes)
        file_size = data_size + 36

        wav_buffer = io.BytesIO()
        wav_buffer.write(b"RIFF")
        wav_buffer.write(struct.pack("<I", file_size))
        wav_buffer.write(b"WAVE")
        wav_buffer.write(b"fmt ")
        wav_buffer.write(struct.pack("<I", 16))  # Chunk size
        wav_buffer.write(struct.pack("<H", 1))  # Audio format (PCM)
        wav_buffer.write(struct.pack("<H", channels))
        wav_buffer.write(struct.pack("<I", sample_rate))
        wav_buffer.write(struct.pack("<I", byte_rate))
        wav_buffer.write(struct.pack("<H", block_align))
        wav_buffer.write(struct.pack("<H", bits_per_sample))
        wav_buffer.write(b"data")
        wav_buffer.write(struct.pack("<I", data_size))
        wav_buffer.write(pcm_bytes)

        return base64.b64encode(wav_buffer.getvalue()).decode("ascii")

    def create_session(
        self,
        callbacks: TtsCallbacks | None = None,
    ) -> TtsSession:
        """
        Create a new TTS session for streaming synthesis.

        Args:
            callbacks: Callbacks for synthesis events

        Returns:
            TtsSession instance
        """
        session_id = str(uuid.uuid4())
        return TtsSession(session_id, callbacks or TtsCallbacks())
