import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from nonebot import get_driver, logger

from ..._shared.settings import settings_store
from ..config import pixiv_settings
from ..cookie import migrate_legacy_cookie
from ..spiderPixiv import PixivSpider

_spider: Optional[PixivSpider] = None
_init_lock = asyncio.Lock()


async def get_spider() -> PixivSpider:
    """返回进程内唯一的 PixivSpider，首次调用时加载状态。"""
    global _spider
    if _spider is None:
        async with _init_lock:
            if _spider is None:
                spider = PixivSpider(cookie_source=lambda: str(pixiv_settings.cookie))
                try:
                    if migrate_legacy_cookie(spider.storage, settings_store):
                        logger.info("[Pixiv] 原来用 /p站 登录 保存的 Cookie 已经移到 WebUI「Pixiv」里")
                except Exception as e:  # 搬不过去也不影响使用，在 WebUI 里重新填就行
                    logger.opt(exception=e).warning("[Pixiv] 迁移旧 Cookie 失败")
                await spider.start()
                _spider = spider
    return _spider


@asynccontextmanager
async def get_pixiv_spider():
    yield await get_spider()


async def test_login() -> str:
    """WebUI「Pixiv」的「测试连接」。"""
    spider = await get_spider()
    return await spider.check_login()


settings_store.set_tester("pixiv", test_login)


@get_driver().on_shutdown
async def _close_spider() -> None:
    global _spider
    if _spider is not None:
        await _spider.close()
        _spider = None


__all__ = ["PixivSpider", "get_pixiv_spider", "get_spider"]
