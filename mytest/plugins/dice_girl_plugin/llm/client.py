from contextlib import asynccontextmanager

import httpx
from nonebot import get_driver, logger

from ..config import config, get_data_path

_ai_client: httpx.AsyncClient | None = None
_ai_runtime_enabled: bool = bool(config.ai_enabled)
# 管理员通过 /dice AI 开启|关闭 设置的开关会写到这里，重启后仍然生效。
_AI_SWITCH_FILE = get_data_path("ai_enabled.txt")


def _load_runtime_switch() -> None:
    global _ai_runtime_enabled
    try:
        if _AI_SWITCH_FILE.exists():
            _ai_runtime_enabled = _AI_SWITCH_FILE.read_text(encoding="utf-8").strip() == "1"
    except OSError as e:
        logger.warning("[AI] 读取 AI 开关失败，使用配置文件的值: {}", e)


_load_runtime_switch()


def chat_url() -> str:
    return f"{config.ai_base_url.rstrip('/')}/chat/completions"


def has_ai_api_key() -> bool:
    return bool(config.ai_api_key and config.ai_api_key.strip())


def is_ai_enabled() -> bool:
    return _ai_runtime_enabled


def set_ai_enabled(enabled: bool) -> bool:
    global _ai_runtime_enabled
    _ai_runtime_enabled = enabled
    try:
        _AI_SWITCH_FILE.write_text("1" if enabled else "0", encoding="utf-8")
    except OSError as e:
        logger.warning("[AI] 保存 AI 开关失败，重启后会恢复为配置值: {}", e)
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
    if not has_ai_api_key():
        logger.warning(
            "[AI] API key is empty. Set DICE_GIRL_AI_API_KEY in the active env file."
        )
    # 即使当前关闭也创建共享客户端，这样管理员运行时开启 AI 后不必每次新建连接。
    _ai_client = httpx.AsyncClient(
        timeout=httpx.Timeout(config.ai_timeout, connect=10.0)
    )


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
