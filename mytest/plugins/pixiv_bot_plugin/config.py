import os
from pathlib import Path
from typing import Optional

from nonebot import get_driver
from pydantic import BaseModel


class PixivBotConfig(BaseModel):
    data_dir: Optional[Path] = None


def _load_config() -> PixivBotConfig:
    raw_config = get_driver().config
    return PixivBotConfig(
        data_dir=getattr(raw_config, "pixiv_bot_data_dir", None)
        or os.getenv("PIXIV_BOT_DATA_DIR"),
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
            data_dir = Path.cwd() / "data" / "pixiv_bot_plugin"

    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / filename
