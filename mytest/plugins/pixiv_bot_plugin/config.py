import os
from pathlib import Path

from nonebot import get_driver, logger

from .._shared.settings import SettingField, SettingSection, register

pixiv_settings = register(
    SettingSection(
        id="pixiv",
        title="Pixiv",
        description="搜图、每日一图等。登录 Cookie、喜好标签仍用原来的 p站 指令管理。",
        order=50,
        fields=[
            SettingField(
                "image_proxy",
                "图片反代域名",
                default="i.isekai-inn.uno",
                help="替换 i.pximg.net；留空表示直连（需要服务器能访问 Pixiv）",
                env=("pixiv_bot_image_proxy",),
            ),
        ],
    )
)


class _Config:
    data_dir = (
        str(getattr(get_driver().config, "pixiv_bot_data_dir", "") or "")
        or os.environ.get("PIXIV_BOT_DATA_DIR")
        or None
    )

    @property
    def image_proxy_host(self) -> str:
        return str(pixiv_settings.image_proxy).strip()


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
