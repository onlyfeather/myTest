import asyncio
import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

nonebot.init(log_level="INFO")

from mytest.plugins.pixiv_bot_plugin.parser.search import build_search_params  # noqa: E402


def test_build_search_params_splits_bookmark_tag():
    params = build_search_params("くすぐり 500users入り", 1, "random")
    assert params is not None
    assert params.display_tags == "くすぐり"
    assert params.min_bookmarks == 500
    assert params.search_tags == "くすぐり 500users入り"
    assert build_search_params("500users入り", 1, "random") is None


def test_search_illustrations_filters_blocked_tags_including_popular(tmp_path):
    from mytest.plugins.pixiv_bot_plugin.pixiv_storage import PixivStorage
    from mytest.plugins.pixiv_bot_plugin.spiderPixiv import PixivSpider

    class API:
        async def search(self, keyword, page=1, *, order="date_d"):
            return {
                "illustManga": {"data": [{"id": "1", "tags": ["風景"]}], "total": 1},
                "popular": {
                    "recent": [{"id": "2", "tags": ["R-18"]}, {"id": "3", "tags": ["風景"]}],
                    "permanent": [{"id": "4", "tags": ["r18", "女の子"]}],
                },
            }

        def proxy_url(self, url):
            return url

    spider = PixivSpider(PixivStorage(tmp_path / "p.db"), api=API())
    spider.library.load()
    spider.library.tags.replace("blocked", ["R-18"])
    data = asyncio.run(spider.search_illustrations("風景"))
    assert sorted(i["id"] for i in data["illusts"]) == ["1", "3"]
