import asyncio
import json
import logging
import math
import os
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from langchain_openai import ChatOpenAI
from sqlalchemy import select

from app.config.database import _async_session_factory
from app.config.settings import get_settings
from app.core.errors import BusinessException, ErrorCode
from app.infrastructure.ai.api_key_encryption import get_encryption_service
from app.models.llm_provider import LlmGlobalSettingEntity, LlmProviderEntity


log = logging.getLogger(__name__)
settings = get_settings()

# 进程级单例：LLM 客户端缓存（跨 WS 会话复用）
_client_cache: dict[str, Any] = {}
_httpx_sync_client_cache: dict[str, httpx.Client] = {}  # ChatOpenAI 需要同步 Client
_httpx_async_client_cache: dict[str, httpx.AsyncClient] = {}  # 异步操作
_embedding_client_cache: dict[str, Any] = {}
_direct_http2_client_cache: dict[str, httpx.AsyncClient] = {}
_provider_config_cache: dict[str, dict[str, Any]] = {}
_default_chat_provider_id: str | None = None
_default_embedding_provider_id: str | None = None
_registry_lock = asyncio.Lock()

# Task 1: 设置环境变量禁用 LangChain 的 TCP keepalive transport 注入，避免代理握手延迟
os.environ.setdefault("LANGCHAIN_OPENAI_TCP_KEEPALIVE", "0")


async def reload() -> None:
    global settings, _provider_config_cache
    global _default_chat_provider_id, _default_embedding_provider_id

    async with _registry_lock:
        provider_configs, default_chat, default_embedding = await _load_registry_snapshot()
        old_sync_clients = list(_httpx_sync_client_cache.values())
        old_async_clients = list(_httpx_async_client_cache.values())
        old_direct_clients = list(_direct_http2_client_cache.values())

        get_settings.cache_clear()
        settings = get_settings()
        _provider_config_cache = provider_configs
        _default_chat_provider_id = default_chat
        _default_embedding_provider_id = default_embedding
        _client_cache.clear()
        _embedding_client_cache.clear()
        _httpx_sync_client_cache.clear()
        _httpx_async_client_cache.clear()
        _direct_http2_client_cache.clear()

    for client in old_sync_clients:
        try:
            client.close()
        except Exception as exc:
            log.warning("关闭旧同步 LLM 客户端失败: %s", exc)
    for client in old_async_clients + old_direct_clients:
        try:
            await client.aclose()
        except Exception as exc:
            log.warning("关闭旧异步 LLM 客户端失败: %s", exc)

    log.info(
        "LLM Provider 配置已重新加载: providers=%d chat=%s embedding=%s",
        len(provider_configs),
        default_chat,
        default_embedding,
    )


async def _load_registry_snapshot() -> tuple[dict[str, dict[str, Any]], str, str]:
    encryption_service = get_encryption_service()
    async with _async_session_factory() as session:
        result = await session.execute(
            select(LlmProviderEntity).where(LlmProviderEntity.enabled.is_(True))
        )
        providers = result.scalars().all()
        setting = await session.get(LlmGlobalSettingEntity, 1)

    configs: dict[str, dict[str, Any]] = {}
    for provider in providers:
        try:
            api_key = encryption_service.decrypt(
                provider.api_key_nonce,
                provider.api_key_ciphertext,
            )
        except Exception as exc:
            raise BusinessException(
                ErrorCode.PROVIDER_CONFIG_READ_FAILED,
                f"解密 Provider '{provider.id}' API Key 失败，请检查 APP_AI_CONFIG_ENCRYPTION_KEY",
            ) from exc
        configs[provider.id] = {
            "base_url": provider.base_url,
            "api_key": api_key,
            "model": provider.model,
            "embedding_model": provider.embedding_model,
            "embedding_dimensions": provider.embedding_dimensions,
            "supports_embedding": provider.supports_embedding,
            "temperature": provider.temperature,
        }

    if not configs:
        raise BusinessException(
            ErrorCode.PROVIDER_CONFIG_READ_FAILED,
            "没有可用的 LLM Provider 配置",
        )

    default_chat = (
        setting.default_chat_provider_id if setting else settings.ai.default_provider
    )
    default_embedding = (
        setting.default_embedding_provider_id
        if setting
        else settings.ai.default_embedding_provider
    )
    if default_chat not in configs:
        raise BusinessException(
            ErrorCode.PROVIDER_CONFIG_READ_FAILED,
            f"默认聊天 Provider '{default_chat}' 不存在或已禁用",
        )
    embedding_config = configs.get(default_embedding)
    if not embedding_config or not embedding_config["supports_embedding"]:
        raise BusinessException(
            ErrorCode.PROVIDER_CONFIG_READ_FAILED,
            f"默认向量 Provider '{default_embedding}' 不存在、已禁用或不支持 Embedding",
        )
    if not embedding_config["embedding_model"]:
        raise BusinessException(
            ErrorCode.PROVIDER_CONFIG_READ_FAILED,
            f"默认向量 Provider '{default_embedding}' 未配置 Embedding 模型",
        )
    return configs, default_chat, default_embedding


def _httpx_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        timeout=float(os.getenv("LLM_HTTP_TIMEOUT", "30")),
        connect=float(os.getenv("LLM_CONNECT_TIMEOUT", "10")),
        read=float(os.getenv("LLM_READ_TIMEOUT", "300")),
        write=float(os.getenv("LLM_WRITE_TIMEOUT", "10")),
        pool=float(os.getenv("LLM_POOL_TIMEOUT", "5")),
    )


def _message_content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return 0


def _request_payload_summary(request: httpx.Request) -> dict[str, Any]:
    try:
        payload = json.loads(request.content)
    except (TypeError, ValueError):
        return {}

    messages = payload.get("messages") or []
    message_chars = sum(
        _message_content_chars(message.get("content"))
        for message in messages
        if isinstance(message, dict)
    )
    message_bytes = sum(
        len(str(message.get("content") or "").encode("utf-8"))
        for message in messages
        if isinstance(message, dict)
    )
    return {
        "model": payload.get("model", "<未设置>"),
        "max_tokens": payload.get(
            "max_tokens", payload.get("max_completion_tokens", "<服务端默认>")
        ),
        "stream": payload.get("stream", False),
        "temperature": payload.get("temperature", "<服务端默认>"),
        "messages": len(messages),
        "message_chars": message_chars,
        "estimated_tokens": math.ceil(message_bytes / 4),
    }


def _log_http_client_config(kind: str, provider: str, timeout: httpx.Timeout, limits: httpx.Limits) -> None:
    log.info(
        "[LLM_HTTP] client_config | kind=%s provider=%s timeout(connect=%.1fs read=%.1fs write=%.1fs pool=%.1fs) limits(max_connections=%s max_keepalive=%s keepalive_expiry=%s)",
        kind, provider, timeout.connect, timeout.read, timeout.write, timeout.pool,
        limits.max_connections, limits.max_keepalive_connections, limits.keepalive_expiry,
    )


def _safe_proxy_value(value: str | None) -> str:
    if not value:
        return "<unset>"
    parsed = urlsplit(value)
    if not parsed.hostname:
        return value
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if parsed.username:
        host = f"{parsed.username}:***@{host}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def _log_proxy_environment() -> None:
    names = (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    )
    log.info(
        "[LLM_HTTP] proxy_env | %s",
        " ".join(f"{name}={_safe_proxy_value(os.environ.get(name))}" for name in names),
    )


def _create_httpx_sync_client(provider: str) -> httpx.Client:
    """创建同步 httpx Client。"""
    if provider in _httpx_sync_client_cache:
        return _httpx_sync_client_cache[provider]

    # 使用连接池复用，避免每次请求重新建立连接
    timeout = _httpx_timeout()
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)
    client = httpx.Client(timeout=timeout, limits=limits, trust_env=False)
    _httpx_sync_client_cache[provider] = client
    log.info("[ChatClient] Created sync httpx client for provider=%s", provider)
    _log_http_client_config("sync", provider, timeout, limits)
    return client


def _create_httpx_async_client(provider: str) -> httpx.AsyncClient:
    """创建异步 httpx AsyncClient。"""
    if provider in _httpx_async_client_cache:
        return _httpx_async_client_cache[provider]

    timeout = _httpx_timeout()
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)
    client = httpx.AsyncClient(timeout=timeout, limits=limits, trust_env=False)
    _httpx_async_client_cache[provider] = client
    log.info("[ChatClient] Created async httpx client for provider=%s", provider)
    _log_http_client_config("async", provider, timeout, limits)
    return client


async def init_llm_clients() -> None:
    """
    Task 1: 应用启动时初始化所有 LLM 客户端（进程级单例）。

    在 lifespan startup 中调用，确保连接池在首次请求前已建立。
    """
    log.info("[ChatClient] Initializing LLM clients at startup...")

    # 预热 dashscope 客户端
    try:
        await get_chat_client("dashscope")
        log.info("[ChatClient] DashScope client warmed up")
    except Exception as e:
        log.warning("[ChatClient] Failed to warm up DashScope client: %s", e)

    # 预热 lmstudio 客户端（如果配置了）
    try:
        await get_chat_client("lmstudio")
        log.info("[ChatClient] LMStudio client warmed up")
    except Exception as e:
        log.debug("[ChatClient] LMStudio client not configured: %s", e)

    log.info("[ChatClient] LLM clients initialization complete")

    # Task B2: 启动后探测 DashScope 原始延迟
    await probe_dashscope_latency()

    # Task 2 探针：直接 httpx 流式请求（对比 LangChain 路径）
    await probe_raw_dashscope()


async def probe_dashscope_latency() -> None:
    """
    Task B2: 直连 DashScope API 探针，绕过 LangChain。

    用于判断延迟是 LangChain 层引入的还是 DashScope 服务本身的。
    """
    import time
    import httpx

    settings_local = get_settings()
    api_key = settings_local.ai.bailian_api_key
    model = settings_local.ai.provider_dashscope_model or "qwen3.6-flash"

    if not api_key:
        log.info("[PROBE] DashScope API key not configured, skipping latency probe")
        return

    base_url = settings_local.ai.provider_dashscope_base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"

    log.info("[PROBE] Starting DashScope latency probe (model=%s)", model)

    for attempt in range(2):
        t0 = time.perf_counter_ns()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                # 使用 stream() 而非 post()，真正测量流式首字节延迟
                async with client.stream(
                    "POST",
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                        "max_tokens": 10,
                    },
                ) as resp:
                    t_after_connect = time.perf_counter_ns()

                    if resp.status_code != 200:
                        log.warning("[PROBE] probe failed with status %d", resp.status_code)
                        continue

                    # 读取第一个流式 chunk
                    async for line in resp.aiter_lines():
                        if line.startswith("data:") or line.startswith(":"):
                            ttft_ns = time.perf_counter_ns() - t0
                            connect_ns = t_after_connect - t0
                            log.info(
                                "[PROBE] DashScope raw TTFT=%.0fms | connect=%.0fms | attempt=%d",
                                ttft_ns / 1_000_000, connect_ns / 1_000_000, attempt + 1,
                            )
                            return

        except Exception as e:
            log.warning("[PROBE] probe error: %s", e)

    log.warning("[PROBE] DashScope probe completed (no data received)")


async def probe_raw_dashscope() -> None:
    """
    Task 2 探针：绕过 LangChain，直接用 httpx 测量 DashScope TTFT。
    用于对比 LangChain 路径的延迟。
    """
    import time as time_mod

    settings_local = get_settings()
    api_key = settings_local.ai.bailian_api_key
    model = settings_local.ai.provider_dashscope_model or "qwen3.6-flash"
    base_url = settings_local.ai.provider_dashscope_base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"

    if not api_key:
        log.info("[PROBE_RAW] API key not configured")
        return

    t0 = time_mod.perf_counter_ns()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                    "max_tokens": 1,
                },
            ) as resp:
                connect_ms = (time_mod.perf_counter_ns() - t0) / 1_000_000
                if resp.status_code != 200:
                    log.warning("[PROBE_RAW] status=%d", resp.status_code)
                    return
                async for _ in resp.aiter_bytes():
                    raw_ttft_ms = (time_mod.perf_counter_ns() - t0) / 1_000_000
                    log.info(
                        "[PROBE_RAW] raw_dashscope_ttft=%.0fms | connect=%.0fms | model=%s",
                        raw_ttft_ms, connect_ms, model,
                    )
                    return
    except Exception as e:
        log.warning("[PROBE_RAW] error: %s", e)


class _LoggedAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, provider: str, started_ns: int):
        self._stream = stream
        self._provider = provider
        self._started_ns = started_ns
        self._first_chunk = False

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                if not self._first_chunk:
                    self._first_chunk = True
                    now = time.perf_counter_ns()
                    log.info(
                        "[LLM_HTTP] ttfb | provider=%s elapsed=%.1fms perf_ns=%d",
                        self._provider, (now - self._started_ns) / 1_000_000, now,
                    )
                yield chunk
            now = time.perf_counter_ns()
            log.info(
                "[LLM_HTTP] response_complete | provider=%s elapsed=%.1fms perf_ns=%d",
                self._provider, (now - self._started_ns) / 1_000_000, now,
            )
        except Exception as exc:
            log.error(
                "[LLM_HTTP] response_error | provider=%s phase=read exception_type=%s exception=%r elapsed=%.1fms",
                self._provider, type(exc).__name__, exc,
                (time.perf_counter_ns() - self._started_ns) / 1_000_000,
                exc_info=True,
            )
            raise

    async def aclose(self) -> None:
        await self._stream.aclose()


async def get_chat_client(provider_id: str | None = None) -> Any:
    async with _registry_lock:
        return await _get_chat_client_locked(provider_id)


def _resolve_chat_provider_id(provider_id: str | None) -> str | None:
    if (
        provider_id is None
        or not provider_id.strip()
        or provider_id.strip().lower() == "default"
    ):
        return _default_chat_provider_id
    return provider_id


async def _get_chat_client_locked(provider_id: str | None = None) -> Any:
    """
    获取 ChatClient（进程级单例，连接池跨请求复用）。

    Task 1 优化：
    1. httpx 连接池复用，避免每次请求重新建立连接
    2. 分层超时通过环境变量配置，避免无限等待
    3. max_retries=1 避免重试掩盖根因
    4. LANGCHAIN_OPENAI_TCP_KEEPALIVE=0 禁用代理握手延迟
    """
    provider = _resolve_chat_provider_id(provider_id)
    if not provider:
        raise BusinessException(
            ErrorCode.PROVIDER_CONFIG_READ_FAILED,
            "LLM Provider Registry 尚未初始化",
        )
    log.info(
        "[ChatClient] provider_id=%s, resolved_provider=%s, default_provider=%s",
        provider_id,
        provider,
        _default_chat_provider_id,
    )

    # 检查缓存（进程级单例）
    if provider in _client_cache:
        log.debug("[CACHE HIT] LLM client | provider=%s", provider)
        return _client_cache[provider]

    config = _load_provider_config(provider)
    log.info("[ChatClient] Creating new client: base_url=%s, model=%s",
             config["base_url"], config["model"])

    # Task 1: 使用 httpx 连接池复用，并配置分层超时
    _log_proxy_environment()
    sync_http_client = _create_httpx_sync_client(provider)
    async_http_client = _create_httpx_async_client(provider)

    request_timeout = _httpx_timeout()
    client = ChatOpenAI(
        model=config["model"],
        api_key=config["api_key"],
        base_url=config["base_url"],
        temperature=(
            config["temperature"] if config.get("temperature") is not None else 0.2
        ),
        http_client=sync_http_client,  # 复用连接池
        http_async_client=async_http_client,
        timeout=request_timeout,
        max_retries=0,  # 关闭 LangChain 重试，由 asyncio.wait_for 控制超时
    )

    async def _log_request_start(request: httpx.Request) -> None:
        started_ns = time.perf_counter_ns()
        request.extensions["llm_request_start_ns"] = started_ns
        stage_started_ns: dict[str, int] = {}

        async def _trace(event_name: str, info: dict[str, Any]) -> None:
            now_ns = time.perf_counter_ns()
            stage_name, event = event_name.rsplit(".", 1)
            if event == "started":
                stage_started_ns[stage_name] = now_ns
                log.info(
                    "[LLM_HTTP] transport_stage_start | provider=%s stage=%s total_elapsed=%.1fms",
                    provider, stage_name, (now_ns - started_ns) / 1_000_000,
                )
                return
            stage_start_ns = stage_started_ns.get(stage_name, started_ns)
            if event == "failed":
                exception = info.get("exception")
                if isinstance(exception, GeneratorExit):
                    log.debug(
                        "[LLM_HTTP] transport_stage_cancelled | provider=%s stage=%s "
                        "stage_elapsed=%.1fms total_elapsed=%.1fms reason=consumer_closed_stream",
                        provider, stage_name, (now_ns - stage_start_ns) / 1_000_000,
                        (now_ns - started_ns) / 1_000_000,
                    )
                    return
                log.error(
                    "[LLM_HTTP] transport_stage_failed | provider=%s stage=%s stage_elapsed=%.1fms total_elapsed=%.1fms exception_type=%s exception=%r",
                    provider, stage_name, (now_ns - stage_start_ns) / 1_000_000,
                    (now_ns - started_ns) / 1_000_000,
                    type(exception).__name__ if exception else "unknown", exception,
                )
                return
            log.info(
                "[LLM_HTTP] transport_stage_complete | provider=%s stage=%s stage_elapsed=%.1fms total_elapsed=%.1fms",
                provider, stage_name, (now_ns - stage_start_ns) / 1_000_000,
                (now_ns - started_ns) / 1_000_000,
            )

        request.extensions["trace"] = _trace
        try:
            request_bytes = len(request.content or b"")
        except Exception:
            request_bytes = -1
        payload_summary = _request_payload_summary(request)
        request_timeout = request.extensions.get("timeout") or {}
        log.info(
            "[LLM_HTTP] request_start | provider=%s method=%s url=%s request_bytes=%d "
            "model=%s max_tokens=%s stream=%s temperature=%s messages=%s "
            "message_chars=%s estimated_tokens=%s token_estimate_method=utf8_bytes_div_4 "
            "request_timeout(connect=%s read=%s write=%s pool=%s) perf_ns=%d",
            provider, request.method, str(request.url).split("?", 1)[0], request_bytes,
            payload_summary.get("model", "<无法解析>"),
            payload_summary.get("max_tokens", "<无法解析>"),
            payload_summary.get("stream", "<无法解析>"),
            payload_summary.get("temperature", "<无法解析>"),
            payload_summary.get("messages", "<无法解析>"),
            payload_summary.get("message_chars", "<无法解析>"),
            payload_summary.get("estimated_tokens", "<无法解析>"),
            request_timeout.get("connect"), request_timeout.get("read"),
            request_timeout.get("write"), request_timeout.get("pool"), started_ns,
        )

    async def _log_response_headers(response: httpx.Response) -> None:
        received_ns = time.perf_counter_ns()
        started_ns = response.request.extensions.get("llm_request_start_ns", received_ns)
        log.info(
            "[LLM_HTTP] connect_headers_complete | provider=%s status=%d elapsed=%.1fms perf_ns=%d",
            provider,
            response.status_code,
            (received_ns - started_ns) / 1_000_000,
            received_ns,
        )
        response.stream = _LoggedAsyncByteStream(
            response.stream,
            provider,
            started_ns,
        )

    if not getattr(async_http_client, "_llm_hooks_installed", False):
        async_http_client.event_hooks["request"].append(_log_request_start)
        async_http_client.event_hooks["response"].append(_log_response_headers)
        setattr(async_http_client, "_llm_hooks_installed", True)

    _client_cache[provider] = client
    log.info(
        "[LLM_HTTP] timeout_chain | provider=%s outer_question_generation=%.1fs "
        "outer_source=APP_INTERVIEW_QUESTION_GENERATION_TIMEOUT_SECONDS "
        "chat_request=split_httpx "
        "http_connect=%.1fs http_read=%.1fs http_write=%.1fs http_pool=%.1fs",
        provider, float(settings.interview.question_generation_timeout_seconds),
        request_timeout.connect, request_timeout.read,
        request_timeout.write, request_timeout.pool,
    )
    log.info("[ChatClient] ChatClient created: provider=%s model=%s base_url=%s",
             provider, config["model"], config["base_url"])
    return client


async def get_plain_chat_client(provider_id: str | None = None) -> Any:
    """获取同步 ChatClient（复用进程级单例）。"""
    return await get_chat_client(provider_id)


async def get_voice_chat_client(provider_id: str | None = None) -> Any:
    """获取语音面试专用的 ChatClient（复用进程级单例）。"""
    return await get_chat_client(provider_id)


async def get_voice_chat_client_async(provider_id: str | None = None) -> Any:
    async with _registry_lock:
        return await _get_voice_chat_client_async_locked(provider_id)


async def _get_voice_chat_client_async_locked(provider_id: str | None = None) -> Any:
    """
    获取异步 ChatClient（用于流式调用）。

    Task 1: 创建异步版本的 ChatClient，确保 astream 使用正确的连接池。
    """
    provider = _resolve_chat_provider_id(provider_id)
    if not provider:
        raise BusinessException(
            ErrorCode.PROVIDER_CONFIG_READ_FAILED,
            "LLM Provider Registry 尚未初始化",
        )

    config = _load_provider_config(provider)
    from langchain_openai import ChatOpenAI

    # Task 1: 使用禁用代理注入的异步 httpx Client
    async_http_client = _create_httpx_async_client(provider)

    client = ChatOpenAI(
        model=config["model"],
        api_key=config["api_key"],
        base_url=config["base_url"],
        temperature=(
            config["temperature"] if config.get("temperature") is not None else 0.2
        ),
        http_async_client=async_http_client,
        timeout=30.0,
        max_retries=1,
    )
    return client


def _load_provider_config(provider_id: str) -> dict[str, Any]:
  config = _provider_config_cache.get(provider_id)
  if config is None:
    raise BusinessException(
        ErrorCode.PROVIDER_NOT_FOUND,
        f"LLM Provider '{provider_id}' 不存在或已禁用",
    )
  return config


# === DirectDashScopeClient：绕过 LangChain，直连 DashScope ===

async def get_direct_client(provider_id: str | None = None) -> httpx.AsyncClient:
    async with _registry_lock:
        return await _get_direct_client_locked(provider_id)


async def get_direct_client_with_model(
    provider_id: str | None = None,
) -> tuple[httpx.AsyncClient, str]:
  """获取直连 DashScope 的 httpx.AsyncClient 和模型名称。

  用于流式调用场景，调用方需要同时拿到 HTTP 客户端和 model 名。
  """
  async with _registry_lock:
    provider = _resolve_chat_provider_id(provider_id)
    if not provider:
      raise BusinessException(
          ErrorCode.PROVIDER_CONFIG_READ_FAILED,
          "LLM Provider Registry 尚未初始化",
      )
    config = _load_provider_config(provider)
    client = await _get_direct_client_locked(provider)
    return client, config["model"]


async def _get_direct_client_locked(provider_id: str | None = None) -> httpx.AsyncClient:
    """
    获取直连 DashScope 的 httpx AsyncClient（进程级单例，HTTP/2 复用）。

    用于绕过 LangChain astream 的高初始化延迟，实测可将 TTFT 从 ~11s 降至 ~1s。
    """
    provider = _resolve_chat_provider_id(provider_id)
    if not provider:
        raise BusinessException(
            ErrorCode.PROVIDER_CONFIG_READ_FAILED,
            "LLM Provider Registry 尚未初始化",
        )
    if provider in _direct_http2_client_cache:
        return _direct_http2_client_cache[provider]
    config = _load_provider_config(provider)

    client = httpx.AsyncClient(
        base_url=config["base_url"],
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        # 注意：http2=True 需要 h2 包，当前环境未安装，启用会导致请求直接报错，回退 HTTP/1.1
        trust_env=False,
        timeout=httpx.Timeout(
            connect=float(os.getenv("LLM_DIRECT_CONNECT_TIMEOUT", "5")),
            read=float(os.getenv("LLM_DIRECT_READ_TIMEOUT", "60")),
            write=float(os.getenv("LLM_DIRECT_WRITE_TIMEOUT", "10")),
            pool=float(os.getenv("LLM_DIRECT_POOL_TIMEOUT", "5")),
        ),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30.0),
    )
    _direct_http2_client_cache[provider] = client
    log.info("[DirectClient] client created: base_url=%s model=%s", config["base_url"], config["model"])
    return client


async def get_embedding_client(provider_id: str | None = None) -> Any:
  async with _registry_lock:
    from langchain_openai import OpenAIEmbeddings

    provider = provider_id or _default_embedding_provider_id
    if not provider:
      raise BusinessException(
          ErrorCode.PROVIDER_CONFIG_READ_FAILED,
          "LLM Provider Registry 尚未初始化",
      )
    if provider in _embedding_client_cache:
      return _embedding_client_cache[provider]

    config = _load_provider_config(provider)
    if not config["supports_embedding"] or not config["embedding_model"]:
      raise BusinessException(
          ErrorCode.PROVIDER_CONFIG_READ_FAILED,
          f"Provider '{provider}' 未配置可用的 Embedding 模型",
      )
    client = OpenAIEmbeddings(
        model=config["embedding_model"],
        dimensions=config["embedding_dimensions"],
        api_key=config["api_key"],
        base_url=config["base_url"],
        check_embedding_ctx_length=False,
    )
    _embedding_client_cache[provider] = client
    return client


async def clear_cache() -> None:
    """清空客户端缓存（用于测试或配置变更）。"""
    await reload()


async def shutdown_llm_clients() -> None:
    """
    Task 1: 应用关闭时清理 LLM 客户端连接池。

    在 lifespan shutdown 中调用，确保连接正确关闭。
    """
    log.info("[ChatClient] Shutting down LLM clients...")

    async with _registry_lock:
        sync_clients = list(_httpx_sync_client_cache.items())
        async_clients = list(_httpx_async_client_cache.items())
        direct_clients = list(_direct_http2_client_cache.items())
        _client_cache.clear()
        _embedding_client_cache.clear()
        _httpx_sync_client_cache.clear()
        _httpx_async_client_cache.clear()
        _direct_http2_client_cache.clear()

    # 关闭同步 httpx Client
    for provider, client in sync_clients:
        try:
            client.close()
            log.info("[ChatClient] Closed sync httpx client for provider=%s", provider)
        except Exception as e:
            log.warning("[ChatClient] Failed to close sync client for %s: %s", provider, e)

    # 关闭异步 httpx AsyncClient
    for provider, client in async_clients:
        try:
            await client.aclose()
            log.info("[ChatClient] Closed async httpx client for provider=%s", provider)
        except Exception as e:
            log.warning("[ChatClient] Failed to close async client for %s: %s", provider, e)

    for provider, client in direct_clients:
        try:
            await client.aclose()
            log.info("[DirectClient] Closed client for provider=%s", provider)
        except Exception as e:
            log.warning("[DirectClient] Failed to close client for %s: %s", provider, e)

    log.info("[ChatClient] LLM clients shutdown complete")
