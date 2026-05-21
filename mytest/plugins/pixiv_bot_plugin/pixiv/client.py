from contextlib import asynccontextmanager

from ..spiderPixiv import PixivSpider


@asynccontextmanager
async def get_pixiv_spider():
    async with PixivSpider() as spider:
        yield spider


__all__ = ["PixivSpider", "get_pixiv_spider"]
