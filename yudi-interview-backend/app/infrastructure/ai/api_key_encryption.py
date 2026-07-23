import base64
import hashlib
import logging
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


log = logging.getLogger(__name__)

DEV_FALLBACK_KEY = "interview-guide-dev-only-provider-api-key-encryption"
NONCE_BYTES = 12


@dataclass
class EncryptedValue:
    nonce: str
    ciphertext: str


class ApiKeyEncryptionService:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, encryption_key: str | None = None):
        if self._initialized:
            return
        self._initialized = True

        configured_key = encryption_key or os.environ.get("APP_AI_CONFIG_ENCRYPTION_KEY")
        if not configured_key:
            log.warning(
                "APP_AI_CONFIG_ENCRYPTION_KEY is not configured; "
                "using development fallback key"
            )
            configured_key = DEV_FALLBACK_KEY

        self._aesgcm = AESGCM(self._resolve_key_bytes(configured_key))

    def _resolve_key_bytes(self, key: str) -> bytes:
        key = key.strip()
        try:
            decoded = base64.b64decode(key)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass
        return self._sha256(key)

    @staticmethod
    def _sha256(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    def encrypt(self, plaintext: str) -> EncryptedValue:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return EncryptedValue(
            nonce=base64.b64encode(nonce).decode("ascii"),
            ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        )

    def decrypt(self, nonce_base64: str, ciphertext_base64: str) -> str:
        nonce = base64.b64decode(nonce_base64)
        ciphertext = base64.b64decode(ciphertext_base64)
        return self._aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


def get_encryption_service() -> ApiKeyEncryptionService:
    from app.config.settings import get_settings
    settings = get_settings()
    return ApiKeyEncryptionService(settings.app_ai_config_encryption_key)
