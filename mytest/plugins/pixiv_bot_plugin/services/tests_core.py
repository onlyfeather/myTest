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


class DailySpider(FakeSpider):
    """每日一图用：喜好标签是中文，Pixiv 往往不给热门列表，只有普通结果。"""

    def __init__(self, bookmarks_by_id, preferred=("风景", "插画")):
        super().__init__(popular=False)
        self.bookmarks_by_id = bookmarks_by_id
        self.preferred_tags = set(preferred)
        self.details = 0

    def get_daily_seed(self, user_qq):
        return 7

    async def get_illust_details(self, illust_id):
        self.details += 1
        return {"bookmarkCount": self.bookmarks_by_id.get(illust_id, 0)}


def _daily(spider, user="42"):
    from mytest.plugins.pixiv_bot_plugin.services import core

    core._daily_cache.clear()
    return asyncio.run(core.daily_recommend(spider, user))


def test_daily_uses_normal_results_when_no_popular_list():
    spider = DailySpider({"3": 5000})
    result = _daily(spider)
    assert result.ok and result.images[0]["id"] == "3"
    assert result.images[0]["urls"] == ["https://example.com/3.png"]


def test_daily_relaxes_threshold_and_caps_requests():
    spider = DailySpider({"1": 400, "2": 900})
    result = _daily(spider)
    assert result.ok and result.images[0]["id"] == "2", "best below 2000 but above 300"
    assert spider.details <= 30

    empty = DailySpider({})
    result = _daily(empty)
    assert not result.ok and "今日推荐" in result.message


def test_daily_same_picture_all_day():
    from mytest.plugins.pixiv_bot_plugin.services import core

    spider = DailySpider({"3": 5000})
    first = _daily(spider)
    searched = len(spider.searched)
    again = asyncio.run(core.daily_recommend(spider, "42"))
    assert again is first and len(spider.searched) == searched


def test_daily_without_preferred_tags():
    result = _daily(DailySpider({}, preferred=()))
    assert not result.ok and "喜好标签" in result.message


def test_popular_lists_respect_blocked_tags():
    from mytest.plugins.pixiv_bot_plugin.spiderPixiv import PixivSpider

    spider = PixivSpider.__new__(PixivSpider)
    spider.blocked_tags = {"R-18"}
    raw = {"body": {
        "illustManga": {"data": [{"id": "1", "tags": ["風景"]}]},
        "popular": {
            "recent": [{"id": "2", "tags": ["R-18"]}, {"id": "3", "tags": ["風景"]}],
            "permanent": [{"id": "4", "tags": ["r-18", "女の子"]}],
        },
    }}
    data = asyncio.run(spider._extract_simplified_data(raw, "風景"))
    assert [i["id"] for i in data["illusts"]] == ["1"]
    assert [i["id"] for i in data["popular"]["recent"]] == ["3"]
    assert data["popular"]["permanent"] == []


def test_popular_mode_falls_back_when_pixiv_has_no_popular_list():
    spider = FakeSpider(bookmarks=800, popular=False)
    params = build_search_params("くすぐり", 2, "popular")
    result = asyncio.run(search_images(spider, params))
    assert result.ok and len(result.images) == 2
