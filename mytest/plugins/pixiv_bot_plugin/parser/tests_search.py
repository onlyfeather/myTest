import nonebot
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

nonebot.init(log_level="INFO")

from mytest.plugins.pixiv_bot_plugin.parser.search import (
    parse_beautiful_text,
    parse_bookmark_filter,
    parse_hot_text,
    parse_latest_text,
    parse_search_text,
)


def assert_equal(actual, expected):
    assert actual == expected, f"expected {expected!r}, got {actual!r}"


def test_bookmark_filter():
    assert_equal(parse_bookmark_filter("收藏500"), 500)
    assert_equal(parse_bookmark_filter("收藏>=1000"), 1000)
    assert_equal(parse_bookmark_filter("500收藏"), 500)
    assert_equal(parse_bookmark_filter("1000users入り"), 1000)
    assert_equal(parse_bookmark_filter("bookmarks>1200"), 1200)
    assert_equal(parse_bookmark_filter("风景"), None)


def test_parse_search_text():
    params = parse_search_text("搜图 风景")
    assert params is not None
    assert_equal(params.display_tags, "风景")
    assert_equal(params.search_tags, "风景")
    assert_equal(params.count, 1)
    assert_equal(params.mode, "random")
    assert_equal(params.min_bookmarks, None)
    assert_equal(params.illust_id, None)

    params = parse_search_text("搜图 风景 3 随机")
    assert params is not None
    assert_equal(params.display_tags, "风景")
    assert_equal(params.count, 3)
    assert_equal(params.mode, "random")

    params = parse_search_text("搜图 风景 收藏500 3")
    assert params is not None
    assert_equal(params.display_tags, "风景")
    assert_equal(params.search_tags, "风景 500users入り")
    assert_equal(params.count, 3)
    assert_equal(params.min_bookmarks, 500)

    params = parse_search_text("搜图 123456789")
    assert params is not None
    assert_equal(params.illust_id, "123456789")
    assert_equal(params.count, 1)

    params = parse_search_text("/p站 搜图 初音,收藏>=1000,9,热门")
    assert params is not None
    assert_equal(params.display_tags, "初音")
    assert_equal(params.search_tags, "初音 1000users入り")
    assert_equal(params.count, 5)
    assert_equal(params.mode, "popular")


def test_parse_latest_text():
    params = parse_latest_text("p站 最新 风景 5 24")
    assert params is not None
    assert_equal(params.tags, "风景")
    assert_equal(params.count, 5)
    assert_equal(params.hours, 24)

    params = parse_latest_text("/p站 最新 风景 20 999")
    assert params is not None
    assert_equal(params.tags, "风景")
    assert_equal(params.count, 10)
    assert_equal(params.hours, 168)


def test_parse_hot_and_beautiful_text():
    assert_equal(parse_hot_text("p站 热门 原神 3"), ("原神", 3))
    assert_equal(parse_hot_text("/p站 热门 原神 9"), ("原神", 5))
    assert_equal(parse_beautiful_text("p站 美图 初音"), "初音")
    assert_equal(parse_beautiful_text("/p站 美图 初音 3 热门 收藏500"), "初音")


if __name__ == "__main__":
    test_bookmark_filter()
    test_parse_search_text()
    test_parse_latest_text()
    test_parse_hot_and_beautiful_text()
    print("parser search tests passed")
