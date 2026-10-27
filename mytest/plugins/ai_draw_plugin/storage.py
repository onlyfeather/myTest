import os
from pathlib import Path

from nonebot import get_driver, logger

from .artists import ArtistStore


def _data_dir() -> Path:
    configured = str(getattr(get_driver().config, "ai_draw_data_dir", "") or "") or os.environ.get("AI_DRAW_DATA_DIR")
    if configured:
        return Path(configured)
    try:
        import nonebot_plugin_localstore as store

        return store.get_plugin_data_dir()
    except Exception as e:
        fallback = Path.cwd() / "data" / "ai_draw_plugin"
        logger.warning(f"[AI画图] localstore 不可用({e})，数据目录回退到 {fallback}")
        return fallback


DATA_DIR = _data_dir()
artist_store = ArtistStore(DATA_DIR / "artists.json")
