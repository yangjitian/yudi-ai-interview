"""
TTS Pool 注册表 - 用于在 POST /api/voice/sessions 阶段预热 TTS 连接池，
让 WebSocket 连接建立时连接池已就绪，实现首帧延迟 < 1s。

设计要点：
1. session_id 维度的预热池：POST /sessions 时为新会话预热 N 个 TTS 连接
2. 预生成的开场白文本：缓存到 registry，WS 握手时直接取出
3. 池生命周期：跟随 session 结束（end_session 或超时）显式关闭
4. 锁粒度：单个 session_id 的池用 per-key 锁，避免阻塞其他会话
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.services.voice.tts_service import TtsPool


log = logging.getLogger(__name__)


# session_id -> 预热的 TtsPool
_pools: dict[str, TtsPool] = {}
# session_id -> 预生成的开场白文本
_openings: dict[str, str] = {}
# session_id -> 预热锁（同一会话的多次预热请求合并）
_warmup_locks: dict[str, asyncio.Lock] = {}
# 顶层锁，保护 _pools/_openings/_warmup_locks 的读写
_registry_lock = asyncio.Lock()


async def warmup_pool_for_session(
    session_id: str,
    tts_service,
    cfg,
) -> TtsPool:
    """
    为指定 session_id 预热 TTS 连接池。幂等：
    - 同一 session_id 的多次调用只预热一次
    - 已就绪的池直接返回

    应在 POST /api/voice/sessions 中调用。
    """
    async with _registry_lock:
        existing = _pools.get(session_id)
        if existing is not None:
            return existing
        per_key_lock = _warmup_locks.get(session_id)
        if per_key_lock is None:
            per_key_lock = asyncio.Lock()
            _warmup_locks[session_id] = per_key_lock

    async with per_key_lock:
        async with _registry_lock:
            existing = _pools.get(session_id)
            if existing is not None:
                return existing

        log.info("[TtsRegistry] Pre-warming TTS pool for session=%s", session_id)
        pool_size = cfg.max_concurrent_tts_per_session or 3
        pool = TtsPool(pool_size=pool_size)
        await pool.warmup()
        async with _registry_lock:
            _pools[session_id] = pool
        return pool


async def get_pool(session_id: str) -> Optional[TtsPool]:
    """获取已预热的池；如未预热则返回 None。"""
    async with _registry_lock:
        return _pools.get(session_id)


async def set_opening(session_id: str, opening_text: str) -> None:
    """缓存预生成的开场白文本。"""
    async with _registry_lock:
        _openings[session_id] = opening_text


async def get_and_clear_opening(session_id: str) -> Optional[str]:
    """取出并清空缓存的开场白。WS 握手后调用，避免重复播放。"""
    async with _registry_lock:
        return _openings.pop(session_id, None)


async def close_pool_for_session(session_id: str) -> None:
    """关闭并移除指定 session 的 TTS 池。"""
    async with _registry_lock:
        pool = _pools.pop(session_id, None)
        _openings.pop(session_id, None)
        lock = _warmup_locks.pop(session_id, None)
    if pool:
        try:
            await pool.close()
        except Exception as e:
            log.warning("[TtsRegistry] Close pool error for session=%s: %s", session_id, e)
    if lock is not None:
        # 让锁可被 GC
        del lock


def reset_for_tests() -> None:
    """仅供测试使用：清空所有缓存。"""
    _pools.clear()
    _openings.clear()
    _warmup_locks.clear()
