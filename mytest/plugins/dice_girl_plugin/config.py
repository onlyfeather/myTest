import os
from pathlib import Path
from typing import Optional

from nonebot import get_driver
from pydantic import BaseModel


class DiceGirlConfig(BaseModel):
    ai_enabled: bool = True
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_timeout: float = 120.0
    ai_json_mode: bool = False
    data_dir: Optional[Path] = None


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
    )


config = _load_config()


def get_data_path(filename: str) -> Path:
    if config.data_dir:
        data_dir = Path(config.data_dir)
    else:
        try:
            import nonebot_plugin_localstore as store

            data_dir = store.get_plugin_data_dir()
        except Exception:
            data_dir = Path.cwd() / "data" / "dice_girl_plugin"

    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / filename
