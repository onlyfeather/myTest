from contextlib import asynccontextmanager

import httpx
from nonebot import get_driver, logger

from ..config import config

_ai_client: httpx.AsyncClient | None = None
_ai_runtime_enabled: bool = bool(config.ai_enabled)


def chat_url() -> str:
    return f"{config.ai_base_url.rstrip('/')}/chat/completions"


def has_ai_api_key() -> bool:
    return bool(config.ai_api_key and config.ai_api_key.strip())


def is_ai_enabled() -> bool:
    return _ai_runtime_enabled


def set_ai_enabled(enabled: bool) -> bool:
    global _ai_runtime_enabled
    _ai_runtime_enabled = enabled
    logger.info("[AI] Runtime AI enabled set to {}", enabled)
    return _ai_runtime_enabled


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.ai_api_key.strip()}"}


def truncate_text(value: str, limit: int = 800) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


@get_driver().on_startup
async def _startup_ai_client():
    global _ai_client
    logger.debug(
        "[AI] Startup config: enabled={}, base_url={}, model={}, timeout={}, json_mode={}, api_key_set={}",
        _ai_runtime_enabled,
        config.ai_base_url,
        config.ai_model,
        config.ai_timeout,
        config.ai_json_mode,
        has_ai_api_key(),
    )
    if not is_ai_enabled():
        logger.info("[AI] Dice Girl AI is disabled by DICE_GIRL_AI_ENABLED")
        return
    if not has_ai_api_key():
        logger.warning(
            "[AI] API key is empty. Set DICE_GIRL_AI_API_KEY in the active env file."
        )
    _ai_client = httpx.AsyncClient(timeout=config.ai_timeout)


@get_driver().on_shutdown
async def _shutdown_ai_client():
    global _ai_client
    if _ai_client:
        logger.debug("[AI] Closing shared HTTP client")
        await _ai_client.aclose()
        _ai_client = None


@asynccontextmanager
async def get_ai_client():
    if _ai_client:
        yield _ai_client
        return

    async with httpx.AsyncClient(timeout=config.ai_timeout) as client:
        yield client
