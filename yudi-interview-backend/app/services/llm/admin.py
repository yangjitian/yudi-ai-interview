import logging
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessException, ErrorCode
from app.infrastructure.ai.api_key_encryption import EncryptedValue, get_encryption_service
from app.infrastructure.ai.provider_registry import get_plain_chat_client, reload
from app.models.llm_provider import LlmGlobalSettingEntity, LlmProviderEntity
from app.utils.timezone_utils import get_beijing_now_naive

log = logging.getLogger(__name__)


def mask_api_key(api_key: Optional[str]) -> str:
    if not api_key or len(api_key) <= 6:
        return "***"
    return api_key[:3] + "***" + api_key[-3:]


class LlmProviderAdminService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_providers(self) -> list[dict]:
        result = await self.session.execute(select(LlmProviderEntity))
        entities = result.scalars().all()
        setting = await self._get_global_setting()

        return [
            {
                "id": e.id,
                "base_url": e.base_url,
                "model": e.model,
                "embedding_model": e.embedding_model,
                "embedding_dimensions": e.embedding_dimensions,
                "supports_embedding": e.supports_embedding,
                "temperature": e.temperature,
                "masked_api_key": mask_api_key(self._decrypt_api_key(e)),
                "default_chat_provider": e.id == setting.get("default_chat_provider_id"),
                "default_embedding_provider": e.id == setting.get("default_embedding_provider_id"),
                "is_enabled": e.enabled,
            }
            for e in entities
        ]

    async def get_provider(self, provider_id: str) -> dict:
        entity = await self.session.get(LlmProviderEntity, provider_id)
        if entity is None:
            raise BusinessException(ErrorCode.PROVIDER_NOT_FOUND)

        setting = await self._get_global_setting()
        return {
            "id": entity.id,
            "base_url": entity.base_url,
            "model": entity.model,
            "embedding_model": entity.embedding_model,
            "embedding_dimensions": entity.embedding_dimensions,
            "supports_embedding": entity.supports_embedding,
            "temperature": entity.temperature,
            "masked_api_key": mask_api_key(self._decrypt_api_key(entity)),
            "default_chat_provider": entity.id == setting.get("default_chat_provider_id"),
            "default_embedding_provider": entity.id == setting.get("default_embedding_provider_id"),
            "is_enabled": entity.enabled,
        }

    async def create_provider(self, req: dict) -> dict:
        existing = await self.session.get(LlmProviderEntity, req["id"])
        if existing:
            raise BusinessException(ErrorCode.PROVIDER_ALREADY_EXISTS, f"Provider '{req['id']}' 已存在")

        api_key = (req.get("api_key") or "").strip()
        if not api_key:
            raise BusinessException(ErrorCode.BAD_REQUEST, "apiKey 不能为空")
        encrypted = self._encrypt_api_key(api_key)

        entity = LlmProviderEntity(
            id=req["id"],
            base_url=req["base_url"],
            api_key_nonce=encrypted.nonce,
            api_key_ciphertext=encrypted.ciphertext,
            model=req["model"],
            embedding_model=req.get("embedding_model"),
            embedding_dimensions=req.get("embedding_dimensions"),
            supports_embedding=req.get("supports_embedding", False),
            temperature=req.get("temperature"),
            enabled=req.get("is_enabled", True),
            builtin=False,
        )
        self.session.add(entity)
        await self.session.flush()
        await self.session.commit()
        await reload()
        return {"id": entity.id}

    async def update_provider(self, provider_id: str, req: dict) -> dict:
        entity = await self.session.get(LlmProviderEntity, provider_id)
        if entity is None:
            raise BusinessException(ErrorCode.PROVIDER_NOT_FOUND)

        if req.get("base_url") is not None:
            entity.base_url = req["base_url"]
        if req.get("api_key") is not None:
            api_key = req["api_key"].strip()
            if not api_key:
                raise BusinessException(ErrorCode.BAD_REQUEST, "apiKey 不能为空")
            encrypted = self._encrypt_api_key(api_key)
            entity.api_key_nonce = encrypted.nonce
            entity.api_key_ciphertext = encrypted.ciphertext
        if req.get("model") is not None:
            entity.model = req["model"]
        if req.get("embedding_model") is not None:
            entity.embedding_model = req["embedding_model"]
        if req.get("embedding_dimensions") is not None:
            entity.embedding_dimensions = req["embedding_dimensions"]
        if req.get("supports_embedding") is not None:
            entity.supports_embedding = req["supports_embedding"]
        if req.get("temperature") is not None:
            entity.temperature = req["temperature"]
        if req.get("is_enabled") is not None:
            entity.enabled = req["is_enabled"]

        setting = await self.session.get(LlmGlobalSettingEntity, 1)
        if setting and setting.default_chat_provider_id == provider_id and not entity.enabled:
            raise BusinessException(ErrorCode.BAD_REQUEST, "默认聊天 Provider 不能禁用")
        if setting and setting.default_embedding_provider_id == provider_id and (
            not entity.enabled
            or not entity.supports_embedding
            or not entity.embedding_model
        ):
            raise BusinessException(
                ErrorCode.BAD_REQUEST,
                "默认向量 Provider 必须启用并配置可用的 Embedding 模型",
            )

        entity.updated_at = get_beijing_now_naive()
        await self.session.flush()
        await self.session.commit()
        await reload()
        return {"id": entity.id}

    async def delete_provider(self, provider_id: str) -> None:
        entity = await self.session.get(LlmProviderEntity, provider_id)
        if entity is None:
            raise BusinessException(ErrorCode.PROVIDER_NOT_FOUND)

        setting = await self.session.get(LlmGlobalSettingEntity, 1)
        if setting and (
            setting.default_chat_provider_id == provider_id
            or setting.default_embedding_provider_id == provider_id
        ):
            raise BusinessException(
                ErrorCode.PROVIDER_DEFAULT_CANNOT_DELETE,
                f"默认 Provider '{provider_id}' 不可删除，请先切换默认 Provider",
            )

        await self.session.delete(entity)
        await self.session.flush()
        await self.session.commit()
        await reload()

    async def test_provider(self, provider_id: str) -> dict:
        entity = await self.session.get(LlmProviderEntity, provider_id)
        if entity is None:
            raise BusinessException(ErrorCode.PROVIDER_NOT_FOUND)

        start = time.time()
        try:
            api_key = self._decrypt_api_key(entity)
            chat = await get_plain_chat_client(provider_id)
            response = await chat.ainvoke("Reply with OK only.")
            latency_ms = int((time.time() - start) * 1000)
            text = response.content if hasattr(response, "content") else str(response)
            return {
                "success": True,
                "message": "连接成功",
                "model": entity.model,
                "latency_ms": latency_ms,
            }
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            log.error("Provider test failed: %s", e)
            return {
                "success": False,
                "message": f"连接失败: {e}",
                "model": entity.model,
                "latency_ms": latency_ms,
            }

    async def get_global_setting(self) -> dict:
        entity = await self.session.get(LlmGlobalSettingEntity, 1)
        if entity is None:
            return {
                "default_chat_provider_id": None,
                "default_embedding_provider_id": None,
                "embedding_dimensions": 1024,
            }
        return {
            "default_chat_provider_id": entity.default_chat_provider_id,
            "default_embedding_provider_id": entity.default_embedding_provider_id,
            "embedding_dimensions": 1024,
        }

    async def update_global_setting(self, req: dict) -> dict:
        entity = await self.session.get(LlmGlobalSettingEntity, 1)
        if entity is None:
            raise BusinessException(
                ErrorCode.PROVIDER_CONFIG_READ_FAILED,
                "LLM 全局设置未初始化",
            )

        if req.get("default_chat_provider_id") is not None:
            provider_id = req["default_chat_provider_id"]
            provider = await self.session.get(LlmProviderEntity, provider_id)
            if provider is None or not provider.enabled:
                raise BusinessException(
                    ErrorCode.BAD_REQUEST,
                    f"Provider '{provider_id}' 不存在或已禁用",
                )
            entity.default_chat_provider_id = provider_id
        if req.get("default_embedding_provider_id") is not None:
            provider_id = req["default_embedding_provider_id"]
            provider = await self.session.get(LlmProviderEntity, provider_id)
            if (
                provider is None
                or not provider.enabled
                or not provider.supports_embedding
                or not provider.embedding_model
            ):
                raise BusinessException(
                    ErrorCode.BAD_REQUEST,
                    f"Provider '{provider_id}' 未配置可用的 Embedding 模型",
                )
            entity.default_embedding_provider_id = provider_id
        entity.updated_at = get_beijing_now_naive()
        await self.session.flush()
        await self.session.commit()
        await reload()
        return await self.get_global_setting()

    async def _get_global_setting(self) -> dict:
        return await self.get_global_setting()

    def _encrypt_api_key(self, plaintext: str) -> EncryptedValue:
        enc = get_encryption_service()
        return enc.encrypt(plaintext)

    def _decrypt_api_key(self, entity: LlmProviderEntity) -> str:
        if not entity.api_key_nonce or not entity.api_key_ciphertext:
            raise BusinessException(
                ErrorCode.PROVIDER_CONFIG_READ_FAILED,
                f"Provider '{entity.id}' 的 API Key 加密数据不完整",
            )
        enc = get_encryption_service()
        try:
            return enc.decrypt(entity.api_key_nonce, entity.api_key_ciphertext)
        except Exception as e:
            raise BusinessException(
                ErrorCode.PROVIDER_CONFIG_READ_FAILED,
                f"解密 Provider '{entity.id}' API Key 失败，请检查 APP_AI_CONFIG_ENCRYPTION_KEY",
            ) from e

    async def get_asr_config(self) -> dict:
        from app.config.settings import get_settings
        settings = get_settings()
        voice = settings.voice_interview
        api_key = voice.app_voice_tts_api_key or settings.bailian_api_key
        return {
            "url": voice.app_voice_asr_url,
            "model": voice.app_voice_asr_model,
            "masked_api_key": mask_api_key(api_key) if api_key else "***",
            "language": voice.app_voice_asr_language,
            "format": voice.app_voice_asr_format,
            "sample_rate": voice.app_voice_asr_sample_rate,
            "enable_turn_detection": voice.app_voice_asr_enable_turn_detection,
            "turn_detection_type": voice.app_voice_asr_turn_detection_type,
            "turn_detection_threshold": voice.app_voice_asr_turn_detection_threshold,
            "turn_detection_silence_duration_ms": voice.app_voice_asr_silence_ms,
        }

    async def update_asr_config(self, req: dict) -> dict:
        from app.config.settings import get_settings
        settings = get_settings()
        voice = settings.voice_interview
        updated = False

        if req.get("url") is not None:
            voice.app_voice_asr_url = req["url"]
            updated = True
        if req.get("model") is not None:
            voice.app_voice_asr_model = req["model"]
            updated = True
        if req.get("language") is not None:
            voice.app_voice_asr_language = req["language"]
            updated = True
        if req.get("format") is not None:
            voice.app_voice_asr_format = req["format"]
            updated = True
        if req.get("sample_rate") is not None:
            voice.app_voice_asr_sample_rate = req["sample_rate"]
            updated = True
        if req.get("enable_turn_detection") is not None:
            voice.app_voice_asr_enable_turn_detection = req["enable_turn_detection"]
            updated = True
        if req.get("turn_detection_type") is not None:
            voice.app_voice_asr_turn_detection_type = req["turn_detection_type"]
            updated = True
        if req.get("turn_detection_threshold") is not None:
            voice.app_voice_asr_turn_detection_threshold = req["turn_detection_threshold"]
            updated = True
        if req.get("turn_detection_silence_duration_ms") is not None:
            voice.app_voice_asr_silence_ms = req["turn_detection_silence_duration_ms"]
            updated = True
        if req.get("api_key") is not None:
            voice.app_voice_tts_api_key = req["api_key"]
            updated = True

        if updated:
            await self._save_voice_config()

        return await self.get_asr_config()

    async def get_tts_config(self) -> dict:
        from app.config.settings import get_settings
        settings = get_settings()
        voice = settings.voice_interview
        api_key = voice.app_voice_tts_api_key or settings.bailian_api_key
        return {
            "model": voice.app_voice_tts_model,
            "masked_api_key": mask_api_key(api_key) if api_key else "***",
            "voice": voice.app_voice_tts_voice,
            "format": voice.app_voice_tts_format,
            "sample_rate": voice.app_voice_tts_sample_rate,
            "mode": voice.app_voice_tts_mode,
            "language_type": voice.app_voice_tts_language_type,
            "speech_rate": voice.app_voice_tts_speech_rate,
            "volume": voice.app_voice_tts_volume,
        }

    async def update_tts_config(self, req: dict) -> dict:
        from app.config.settings import get_settings
        settings = get_settings()
        voice = settings.voice_interview
        updated = False

        if req.get("model") is not None:
            voice.app_voice_tts_model = req["model"]
            updated = True
        if req.get("voice") is not None:
            voice.app_voice_tts_voice = req["voice"]
            updated = True
        if req.get("format") is not None:
            voice.app_voice_tts_format = req["format"]
            updated = True
        if req.get("sample_rate") is not None:
            voice.app_voice_tts_sample_rate = req["sample_rate"]
            updated = True
        if req.get("mode") is not None:
            voice.app_voice_tts_mode = req["mode"]
            updated = True
        if req.get("language_type") is not None:
            voice.app_voice_tts_language_type = req["language_type"]
            updated = True
        if req.get("speech_rate") is not None:
            voice.app_voice_tts_speech_rate = req["speech_rate"]
            updated = True
        if req.get("volume") is not None:
            voice.app_voice_tts_volume = req["volume"]
            updated = True
        if req.get("api_key") is not None:
            voice.app_voice_tts_api_key = req["api_key"]
            updated = True

        if updated:
            await self._save_voice_config()

        return await self.get_tts_config()

    async def test_asr_config(self) -> dict:
        from app.config.settings import get_settings
        settings = get_settings()
        voice = settings.voice_interview
        import socket

        try:
            from urllib.parse import urlparse
            ws_url = voice.app_voice_asr_url or "wss://dashscope.cn"
            parsed = urlparse(ws_url)
            host = parsed.hostname or "dashscope.cn"
            port = parsed.port or (443 if parsed.scheme == "wss" else 80)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host, port))
            sock.close()
            return {
                "success": True,
                "message": f"ASR WebSocket 连接成功: {host}",
                "model": voice.app_voice_asr_model,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"ASR 连接失败: {e}",
                "model": voice.app_voice_asr_model,
            }

    async def _save_voice_config(self) -> None:
        from app.config.settings import ENV_FILE, get_settings
        settings = get_settings()
        voice = settings.voice_interview

        if not ENV_FILE.exists():
            return

        try:
            content = ENV_FILE.read_text(encoding="utf-8")
            lines = content.splitlines()
            new_lines = []
            prefix_map = {
                "APP_VOICE_ASR_URL": str(voice.app_voice_asr_url),
                "APP_VOICE_ASR_MODEL": str(voice.app_voice_asr_model),
                "APP_VOICE_ASR_LANGUAGE": str(voice.app_voice_asr_language),
                "APP_VOICE_ASR_FORMAT": str(voice.app_voice_asr_format),
                "APP_VOICE_ASR_SAMPLE_RATE": str(voice.app_voice_asr_sample_rate),
                "APP_VOICE_ASR_ENABLE_TURN_DETECTION": str(voice.app_voice_asr_enable_turn_detection),
                "APP_VOICE_ASR_TURN_DETECTION_TYPE": str(voice.app_voice_asr_turn_detection_type),
                "APP_VOICE_ASR_TURN_DETECTION_THRESHOLD": str(voice.app_voice_asr_turn_detection_threshold),
                "APP_VOICE_ASR_SILENCE_MS": str(voice.app_voice_asr_silence_ms),
                "APP_VOICE_TTS_API_KEY": str(voice.app_voice_tts_api_key),
                "APP_VOICE_TTS_MODEL": str(voice.app_voice_tts_model),
                "APP_VOICE_TTS_VOICE": str(voice.app_voice_tts_voice),
                "APP_VOICE_TTS_FORMAT": str(voice.app_voice_tts_format),
                "APP_VOICE_TTS_SAMPLE_RATE": str(voice.app_voice_tts_sample_rate),
                "APP_VOICE_TTS_MODE": str(voice.app_voice_tts_mode),
                "APP_VOICE_TTS_LANGUAGE_TYPE": str(voice.app_voice_tts_language_type),
                "APP_VOICE_TTS_SPEECH_RATE": str(voice.app_voice_tts_speech_rate),
                "APP_VOICE_TTS_VOLUME": str(voice.app_voice_tts_volume),
            }

            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    new_lines.append(line)
                    continue
                key = stripped.split("=", 1)[0]
                if key in prefix_map:
                    new_lines.append(f"{key}={prefix_map[key]}")
                else:
                    new_lines.append(line)

            ENV_FILE.write_text("\n".join(new_lines), encoding="utf-8")
            log.info("Voice config saved to %s", ENV_FILE)
        except Exception as e:
            log.warning("Failed to save voice config: %s", e)
