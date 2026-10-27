from pathlib import Path
from typing import Optional

from nonebot import get_plugin_config, logger
from pydantic import BaseModel


class PixivBotConfig(BaseModel):
    pixiv_bot_data_dir: Optional[Path] = None
    # 替换 i.pximg.net 的反代域名；设为空字符串则直连 i.pximg.net（需要服务器能访问 Pixiv）。
    pixiv_bot_image_proxy: str = "i.isekai-inn.uno"


_raw = get_plugin_config(PixivBotConfig)


class _Config:
    data_dir = _raw.pixiv_bot_data_dir
    image_proxy_host = _raw.pixiv_bot_image_proxy.strip()


config = _Config()


def _resolve_data_dir() -> Path:
    if config.data_dir:
        return Path(config.data_dir)
    try:
        import nonebot_plugin_localstore as store

        return store.get_plugin_data_dir()
    except Exception as e:
        fallback = Path.cwd() / "data" / "pixiv_bot_plugin"
        logger.warning(f"[Pixiv] localstore 不可用({e})，数据目录回退到 {fallback}")
        return fallback


DATA_DIR = _resolve_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f"[Pixiv] 数据目录: {DATA_DIR.resolve()}")


def get_data_path(filename: str) -> Path:
    return DATA_DIR / filename
