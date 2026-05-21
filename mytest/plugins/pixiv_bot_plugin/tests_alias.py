import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

nonebot.init(log_level="INFO")

from mytest.plugins.pixiv_bot_plugin.spiderPixiv import PixivSpider


def assert_equal(actual, expected):
    assert actual == expected, f"expected {expected!r}, got {actual!r}"


def test_alias_lookup_is_normalized():
    spider = PixivSpider()
    spider.favorite_authors = {
        "123": {
            "name": "Alice",
            "aliases": ["Alice Sensei", " 老王 "],
            "added_time": "2026-01-01T00:00:00",
            "last_check": None,
        }
    }
    spider._update_alias_mapping()

    assert_equal(spider.search_author_by_alias("Alice Sensei"), "123")
    assert_equal(spider.search_author_by_alias("alice sensei"), "123")
    assert_equal(spider.search_author_by_alias("  ALICE   SENSEI  "), "123")
    assert_equal(spider.search_author_by_alias("老王"), "123")
    assert_equal(spider.search_author_by_alias("不存在"), None)


def test_alias_add_remove_uses_normalized_match():
    spider = PixivSpider()
    spider.favorite_authors = {
        "123": {
            "name": "Alice",
            "aliases": [],
            "added_time": "2026-01-01T00:00:00",
            "last_check": None,
        }
    }
    spider.save_favorite_authors = lambda: True
    spider._update_alias_mapping()

    assert_equal(spider.add_author_alias("123", "Alice Sensei"), True)
    assert_equal(spider.add_author_alias("123", " alice   sensei "), False)
    assert_equal(spider.search_author_by_alias("ALICE SENSEI"), "123")
    assert_equal(spider.remove_author_alias("123", "alice sensei"), True)
    assert_equal(spider.search_author_by_alias("Alice Sensei"), None)


if __name__ == "__main__":
    test_alias_lookup_is_normalized()
    test_alias_add_remove_uses_normalized_match()
    print("pixiv alias tests passed")
