from contextlib import asynccontextmanager

from nonebot import logger

from ..._shared.services.http import http_client
from ..config import config, dice_settings, get_data_path

# 旧版本把 /dice AI 开关写在这个文件里，第一次启动时迁移到配置中心。
_LEGACY_SWITCH_FILE = get_data_path("ai_enabled.txt")


def _migrate_legacy_switch() -> None:
    try:
        if not _LEGACY_SWITCH_FILE.exists():
            return
        from ..._shared.settings import settings_store

        if settings_store.resolve("dice_girl", "ai_enabled")[1] != "saved":
            enabled = _LEGACY_SWITCH_FILE.read_text(encoding="utf-8").strip() == "1"
            dice_settings.set("ai_enabled", enabled)
            logger.info("[AI] 已把旧的 AI 开关（{}）迁移到配置中心", "开" if enabled else "关")
        _LEGACY_SWITCH_FILE.rename(_LEGACY_SWITCH_FILE.with_suffix(".txt.migrated"))
    except OSError as e:
        logger.warning("[AI] 迁移旧的 AI 开关失败: {}", e)


_migrate_legacy_switch()


def chat_url() -> str:
    return f"{config.ai_base_url.rstrip('/')}/chat/completions"


def has_ai_api_key() -> bool:
    return bool(config.ai_api_key and config.ai_api_key.strip())


def is_ai_enabled() -> bool:
    return config.ai_enabled


def set_ai_enabled(enabled: bool) -> bool:
    dice_settings.set("ai_enabled", enabled)
    logger.info("[AI] Runtime AI enabled set to {}", enabled)
    return config.ai_enabled


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.ai_api_key.strip()}"}


def truncate_text(value: str, limit: int = 800) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


@asynccontextmanager
async def get_ai_client():
    # 每次按当前配置建立连接，WebUI 里改了代理或超时立即生效。
    async with http_client(config.ai_proxy, config.ai_timeout) as client:
        yield client
