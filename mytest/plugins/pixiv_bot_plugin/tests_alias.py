import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

nonebot.init(log_level="INFO")

from mytest.plugins.pixiv_bot_plugin.pixiv_storage import PixivStorage
from mytest.plugins.pixiv_bot_plugin.spiderPixiv import PixivSpider


def assert_equal(actual, expected):
    assert actual == expected, f"expected {expected!r}, got {actual!r}"


def _spider(tmp_dir: Path) -> PixivSpider:
    return PixivSpider(storage=PixivStorage(tmp_dir / "pixiv_test.db"))


def test_alias_lookup_is_normalized(tmp_path: Path):
    spider = _spider(tmp_path)
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


def test_alias_add_remove_uses_normalized_match(tmp_path: Path):
    spider = _spider(tmp_path)
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


def test_error_reason_is_isolated_per_task(tmp_path: Path):
    import asyncio

    spider = _spider(tmp_path)

    async def worker(reason: str, delay: float) -> str | None:
        spider.last_error_reason = reason
        await asyncio.sleep(delay)
        return spider.last_error_reason

    async def main():
        return await asyncio.gather(worker("a", 0.02), worker("b", 0.0))

    assert_equal(asyncio.run(main()), ["a", "b"])


def test_quality_score_persists(tmp_path: Path):
    spider = _spider(tmp_path)
    assert_equal(spider.set_min_quality_score(150), 100)
    reloaded = _spider(tmp_path)
    reloaded.load_settings()
    assert_equal(reloaded.min_quality_score, 100.0)


def test_parse_pixiv_time_handles_offsets(tmp_path: Path):
    spider = _spider(tmp_path)
    jst = spider.parse_pixiv_time("2025-11-22T12:00:00+09:00")
    utc = spider.parse_pixiv_time("2025-11-22T03:00:00Z")
    assert jst is not None and jst == utc
    assert_equal(spider.parse_pixiv_time("bad"), None)


if __name__ == "__main__":
    import tempfile

    for test in (
        test_alias_lookup_is_normalized,
        test_alias_add_remove_uses_normalized_match,
        test_error_reason_is_isolated_per_task,
        test_quality_score_persists,
        test_parse_pixiv_time_handles_offsets,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            test(Path(tmp))
    print("pixiv alias tests passed")
