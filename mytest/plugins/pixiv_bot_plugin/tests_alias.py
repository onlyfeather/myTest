import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

nonebot.init(log_level="INFO")

from mytest.plugins.pixiv_bot_plugin.library import Library
from mytest.plugins.pixiv_bot_plugin.pixiv_storage import PixivStorage
from mytest.plugins.pixiv_bot_plugin.quality import parse_time
from mytest.plugins.pixiv_bot_plugin.spiderPixiv import PixivSpider


def _library(tmp_dir: Path) -> Library:
    library = Library(PixivStorage(tmp_dir / "pixiv_test.db"))
    library.load()
    return library


def test_alias_lookup_is_normalized(tmp_path: Path):
    authors = _library(tmp_path).authors
    authors.add_many([("123", "Alice")])
    assert authors.set_aliases("123", ["Alice Sensei", " 老王 "]) is None

    assert authors.resolve("Alice Sensei") == "123"
    assert authors.resolve("alice sensei") == "123"
    assert authors.resolve("  ALICE   SENSEI  ") == "123"
    assert authors.resolve("老王") == "123"
    assert authors.resolve("不存在") is None


def test_alias_add_remove_uses_normalized_match(tmp_path: Path):
    authors = _library(tmp_path).authors
    authors.add_many([("123", "")])

    assert authors.add_alias("123", "Alice Sensei") is None
    assert authors.add_alias("123", " alice   sensei ") is not None, "same alias after normalizing"
    assert authors.resolve("ALICE SENSEI") == "123"
    assert authors.remove_alias("alice sensei") is None
    assert authors.resolve("Alice Sensei") is None

    reloaded = _library(tmp_path).authors
    assert reloaded.get("123")["aliases"] == [], "saved to SQLite"


def test_error_reason_is_isolated_per_task(tmp_path: Path):
    import asyncio

    spider = PixivSpider(storage=PixivStorage(tmp_path / "pixiv_test.db"))

    async def worker(reason: str, delay: float) -> str | None:
        spider.last_error_reason = reason
        await asyncio.sleep(delay)
        return spider.last_error_reason

    async def main():
        return await asyncio.gather(worker("a", 0.02), worker("b", 0.0))

    assert asyncio.run(main()) == ["a", "b"]


def test_quality_score_persists(tmp_path: Path):
    assert _library(tmp_path).set_min_quality_score(150) == 100
    assert _library(tmp_path).min_quality_score == 100.0


def test_parse_pixiv_time_handles_offsets():
    jst = parse_time("2025-11-22T12:00:00+09:00")
    utc = parse_time("2025-11-22T03:00:00Z")
    assert jst is not None and jst == utc
    assert parse_time("bad") is None
