from pathlib import Path
from typing import Optional

from nonebot import get_driver
from pydantic import BaseModel


class DiceGirlConfig(BaseModel):
    ai_enabled: bool = True
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    data_dir: Optional[Path] = None


def _load_config() -> DiceGirlConfig:
    raw_config = get_driver().config
    return DiceGirlConfig(
        ai_enabled=getattr(raw_config, "dice_girl_ai_enabled", True),
        ai_api_key=getattr(raw_config, "dice_girl_ai_api_key", "")
        or getattr(raw_config, "ai_api_key", "")
        or getattr(raw_config, "openai_api_key", ""),
        ai_base_url=getattr(raw_config, "dice_girl_ai_base_url", "")
        or getattr(raw_config, "ai_base_url", "")
        or getattr(raw_config, "openai_base_url", "")
        or "https://api.openai.com/v1",
        ai_model=getattr(raw_config, "dice_girl_ai_model", "")
        or getattr(raw_config, "ai_model", "")
        or getattr(raw_config, "openai_model", "")
        or "gpt-4o-mini",
        data_dir=getattr(raw_config, "dice_girl_data_dir", None),
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
