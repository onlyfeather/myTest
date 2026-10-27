import os
from pathlib import Path
from typing import Optional

from nonebot import get_driver, logger
from pydantic import BaseModel


class DiceGirlConfig(BaseModel):
    ai_enabled: bool = True
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_timeout: float = 120.0
    ai_json_mode: bool = False
    data_dir: Optional[Path] = None
    # 群员档案（印象、昵称、记住的事）的隔离方式：group=每个群独立，global=所有群共享。
    member_scope: str = "group"
    # 好感度的隔离方式：global=私聊和所有群共享（原有行为），group=每个群独立。
    affinity_scope: str = "global"
    # 群聊上下文：送进 prompt 的条数、数据库保留条数和保留时长。
    group_context_size: int = 30
    group_keep_messages: int = 200
    group_retention_hours: int = 72
    # 世界书话题触发的限制：同一词条在同一群的冷却秒数、每群每小时最多主动接话次数。
    topic_cooldown_seconds: int = 1800
    topic_replies_per_hour: int = 3
    # 群记忆：攒够这么多条新消息整理一次；冷清的群在消息最旧超过 idle 小时后也整理。
    digest_every_messages: int = 40
    digest_idle_hours: int = 6
    group_events_keep: int = 300
    # Embedding（OpenAI 兼容接口，例如硅基流动）。不填 key 时只用关键词检索。
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_timeout: float = 20.0



def _load_config() -> DiceGirlConfig:
    raw_config = get_driver().config

    def get_config_value(*names: str, default: str = "") -> str:
        for name in names:
            value = getattr(raw_config, name.lower(), "")
            if value not in ("", None):
                return str(value)

            value = os.getenv(name.upper())
            if value not in ("", None):
                return str(value)

        return default

    def get_config_bool(name: str, default: bool = True) -> bool:
        value = getattr(raw_config, name.lower(), None)
        if value is None:
            value = os.getenv(name.upper())

        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off", "关闭"}

    def get_config_float(name: str, default: float) -> float:
        value = getattr(raw_config, name.lower(), None)
        if value is None:
            value = os.getenv(name.upper())

        if value in ("", None):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    return DiceGirlConfig(
        ai_enabled=get_config_bool("dice_girl_ai_enabled", True),
        ai_api_key=get_config_value(
            "dice_girl_ai_api_key",
            "ai_api_key",
            "openai_api_key",
        ),
        ai_base_url=get_config_value(
            "dice_girl_ai_base_url",
            "ai_base_url",
            "openai_base_url",
            default="https://api.openai.com/v1",
        ),
        ai_model=get_config_value(
            "dice_girl_ai_model",
            "ai_model",
            "openai_model",
            default="gpt-4o-mini",
        ),
        ai_timeout=get_config_float("dice_girl_ai_timeout", 120.0),
        ai_json_mode=get_config_bool("dice_girl_ai_json_mode", False),
        data_dir=get_config_value("dice_girl_data_dir") or None,
        member_scope=_choice(get_config_value("dice_girl_member_scope"), ("group", "global"), "group"),
        affinity_scope=_choice(get_config_value("dice_girl_affinity_scope"), ("global", "group"), "global"),
        group_context_size=int(get_config_float("dice_girl_group_context_size", 30)),
        group_keep_messages=int(get_config_float("dice_girl_group_keep_messages", 200)),
        group_retention_hours=int(get_config_float("dice_girl_group_retention_hours", 72)),
        topic_cooldown_seconds=int(get_config_float("dice_girl_topic_cooldown_seconds", 1800)),
        topic_replies_per_hour=int(get_config_float("dice_girl_topic_replies_per_hour", 3)),
        digest_every_messages=max(10, int(get_config_float("dice_girl_digest_every_messages", 40))),
        digest_idle_hours=int(get_config_float("dice_girl_digest_idle_hours", 6)),
        group_events_keep=int(get_config_float("dice_girl_group_events_keep", 300)),
        embedding_api_key=get_config_value("dice_girl_embedding_api_key", "siliconflow_api_key"),
        embedding_base_url=get_config_value(
            "dice_girl_embedding_base_url", default="https://api.siliconflow.cn/v1"
        ),
        embedding_model=get_config_value("dice_girl_embedding_model", default="BAAI/bge-m3"),
        embedding_timeout=get_config_float("dice_girl_embedding_timeout", 20.0),
    )


def _choice(value: str, allowed: tuple[str, ...], default: str) -> str:
    value = (value or "").strip().lower()
    if value and value not in allowed:
        logger.warning(f"[DiceGirl] 配置值 {value!r} 无效，可选 {allowed}，使用 {default!r}")
    return value if value in allowed else default


config = _load_config()


def _resolve_data_dir() -> Path:
    if config.data_dir:
        return Path(config.data_dir)
    try:
        import nonebot_plugin_localstore as store

        return store.get_plugin_data_dir()
    except Exception as e:
        fallback = Path.cwd() / "data" / "dice_girl_plugin"
        logger.warning(f"[DiceGirl] localstore 不可用({e})，数据目录回退到 {fallback}")
        return fallback


DATA_DIR = _resolve_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f"[DiceGirl] 数据目录: {DATA_DIR.resolve()}")


def get_data_path(filename: str) -> Path:
    return DATA_DIR / filename
