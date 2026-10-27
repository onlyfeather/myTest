import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from nonebot import get_driver

from ..spiderPixiv import PixivSpider

_spider: Optional[PixivSpider] = None
_init_lock = asyncio.Lock()


async def get_spider() -> PixivSpider:
    """返回进程内唯一的 PixivSpider，首次调用时加载状态。"""
    global _spider
    if _spider is None:
        async with _init_lock:
            if _spider is None:
                spider = PixivSpider()
                await spider.start()
                _spider = spider
    return _spider


@asynccontextmanager
async def get_pixiv_spider():
    yield await get_spider()


@get_driver().on_shutdown
async def _close_spider() -> None:
    global _spider
    if _spider is not None:
        await _spider.close()
        _spider = None


__all__ = ["PixivSpider", "get_pixiv_spider", "get_spider"]
