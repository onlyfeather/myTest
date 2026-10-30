"""每日一图：每人每天一张，不同的人不同，选定后当天固定。不依赖 NoneBot。

怎么选：用「日期 + QQ 号」算出一个随机数生成器（同一天同一个人每次算出来都一样），
用它决定先试哪个喜好标签、翻第几页、从哪张开始挑。不同的人种子不同，翻到的页和挑到的图也就不同。

为什么还要存起来：光靠种子不能保证一整天都是同一张——作品可能被删、搜索结果每小时都在变（按日期排序时
新作品会把旧作品往后挤），种子算出来的「第 37 页第 12 张」下午就是另一张了。所以第一次选好后存进数据库，
当天之后直接读，重启也不变，也不用再请求 Pixiv。存的记录还能用来保证同一个人 30 天内不会抽到同一张。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .pixiv.errors import PixivError
from .search import SearchRequest

DAILY_TIMEZONE = timezone(timedelta(hours=8))  # 按北京时间换日
NO_REPEAT_DAYS = 30
KEEP_DAYS = 60
MAX_TAGS = 3  # 一次最多试几个喜好标签
MIN_BOOKMARKS = 1000
FALLBACK_MIN_BOOKMARKS = 300


@dataclass
class DailyPick:
    image: dict[str, Any]
    tag: str
    fresh: bool  # 今天第一次选（False 表示读的是已经存好的）


def today(now: datetime | None = None) -> str:
    return (now or datetime.now(DAILY_TIMEZONE)).astimezone(DAILY_TIMEZONE).strftime("%Y%m%d")


def seeded_rng(user_id: str, day: str) -> random.Random:
    digest = hashlib.sha256(f"pixiv-daily:{user_id}:{day}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


class DailyPicker:
    def __init__(self, storage, searcher_factory, preferred_tags, min_quality=lambda: 0.0) -> None:
        """searcher_factory(rng) 返回用这个随机数生成器的 Searcher。"""
        self.storage = storage
        self._searcher = searcher_factory
        self._tags = preferred_tags
        self._min_quality = min_quality

    async def pick(self, user_id: str, *, now: datetime | None = None) -> DailyPick | None:
        day = today(now)
        saved = self.storage.get_daily(user_id, day)
        if saved:
            return DailyPick(saved["image"], saved["tag"], fresh=False)

        tags = sorted(self._tags())
        if not tags:
            return None
        rng = seeded_rng(user_id, day)
        rng.shuffle(tags)
        since = (datetime.strptime(day, "%Y%m%d") - timedelta(days=NO_REPEAT_DAYS)).strftime("%Y%m%d")
        exclude = self.storage.recent_daily_ids(user_id, since)
        searcher = self._searcher(rng)

        last_error: PixivError | None = None
        for min_bookmarks in (MIN_BOOKMARKS, FALLBACK_MIN_BOOKMARKS):
            for tag in tags[:MAX_TAGS]:
                try:
                    outcome = await searcher.search(SearchRequest(
                        keyword=tag, count=1, mode="random", min_bookmarks=min_bookmarks,
                        min_quality=float(self._min_quality()), exclude=exclude, max_rounds=3,
                    ))
                except PixivError as e:
                    if e.reason in ("cookie_expired", "missing_cookie", "rate_limited", "network_error"):
                        raise
                    last_error = e
                    continue
                if outcome.images:
                    image = outcome.images[0]
                    self.storage.save_daily(user_id, day, str(image["id"]), tag, image)
                    self.storage.delete_daily_before(
                        (datetime.strptime(day, "%Y%m%d") - timedelta(days=KEEP_DAYS)).strftime("%Y%m%d"))
                    return DailyPick(image, tag, fresh=True)
        if last_error:
            raise last_error
        return None
