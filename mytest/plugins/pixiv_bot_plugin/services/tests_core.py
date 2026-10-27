import asyncio
import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

nonebot.init(log_level="INFO")

from mytest.plugins.pixiv_bot_plugin.parser.search import build_search_params  # noqa: E402
from mytest.plugins.pixiv_bot_plugin.services.core import search_images  # noqa: E402


class FakeSpider:
    """只有不带 users入り 的搜索才有结果，模拟高收藏作品没打收藏标签的情况。"""

    min_quality_score = 60
    last_error_reason = None

    def __init__(self, bookmarks=800, popular=False):
        self.searched = []
        self.bookmarks = bookmarks
        self.popular = popular

    async def search_illustrations(self, tags):
        self.searched.append(tags)
        if "users入り" in tags:
            return {"illusts": [], "popular": {"recent": [], "permanent": []}}
        illusts = [{"id": str(i), "title": f"t{i}"} for i in range(5)]
        popular = illusts if self.popular else []
        return {"illusts": illusts, "popular": {"recent": popular, "permanent": popular}}

    async def get_illust_details(self, illust_id):
        return {"bookmarkCount": self.bookmarks}

    def calculate_quality_score(self, details):
        return {"total_score": 80}

    async def get_illust_original_urls(self, illust_id):
        return [f"https://example.com/{illust_id}.png"]


def test_build_search_params_splits_bookmark_tag():
    params = build_search_params("くすぐり 500users入り", 1, "random")
    assert params is not None
    assert params.display_tags == "くすぐり"
    assert params.min_bookmarks == 500
    assert params.search_tags == "くすぐり 500users入り"
    assert build_search_params("500users入り", 1, "random") is None


def test_search_retries_without_bookmark_tag():
    spider = FakeSpider(bookmarks=800)
    params = build_search_params("くすぐり 500users入り", 1, "random")
    result = asyncio.run(search_images(spider, params))
    assert result.ok and len(result.images) == 1
    assert spider.searched == ["くすぐり 500users入り", "くすぐり"]


def test_search_retry_still_filters_by_bookmarks():
    spider = FakeSpider(bookmarks=100)
    params = build_search_params("くすぐり 500users入り", 1, "random")
    result = asyncio.run(search_images(spider, params))
    assert not result.ok


def test_popular_mode_falls_back_when_pixiv_has_no_popular_list():
    spider = FakeSpider(bookmarks=800, popular=False)
    params = build_search_params("くすぐり", 2, "popular")
    result = asyncio.run(search_images(spider, params))
    assert result.ok and len(result.images) == 2
