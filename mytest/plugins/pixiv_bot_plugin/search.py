"""搜图的核心：收集候选、过滤、去重、按质量挑选。不依赖 NoneBot。

普通（非会员）账号只能按日期排序（popular_d 要会员），所以「好图」靠两样东西找：
- 「N users入り」标签：读者给收藏数过了 N 的作品打的标签。只有 100、500、1000、5000、10000…这些档位，
  要 2000 收藏时用 1000users入り 先缩小范围，再按作品详情里的真实收藏数筛。
- 随机翻页：按日期排序时，随机的一页就是随机的一段时间，比只看第 1 页（最近上传的 60 张）丰富得多。
  第一次搜某个关键词时只能从第 1 页知道一共几页，所以会马上再跳到随机的一页；之后直接从随机页开始。

其他规则：
- 屏蔽标签、R-18 分级、这个会话最近发过的作品（seen.py）在请求详情之前就过滤掉，不浪费请求。
- 详情（收藏、浏览数）并发请求，有上限；按质量分加权随机抽，好图更容易抽到，但不会每次都是同一张。
- 「最新」模式按时间从新到旧翻，不用收藏标签（新作品还没被打上），只发时间范围内的，按新旧顺序挑。
- 一轮不够时再翻几页，直到凑够或者试过 max_rounds 轮；最后告诉用户是哪一步卡住了。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import quality
from .pixiv.errors import PixivError
from .tag_filter import TagFilter

PAGE_SIZE = 60  # Pixiv 搜索每页 60 个
DEFAULT_MAX_PAGE = 100  # 随机模式最多翻到第几页，WebUI 里可以改
MAX_ROUNDS = 4
DETAIL_BATCH = 12  # 每轮最多查几个作品详情
DETAIL_PARALLEL = 4
LAST_PAGE_MEMORY = 500
# Pixiv 上常见的「N users入り」档位
USERS_TAGS = (100, 500, 1000, 5000, 10000, 30000, 50000)
# 这些错误换一页也不会好，直接停
FATAL = {"cookie_expired", "rate_limited", "missing_cookie", "not_started", "network_error"}


@dataclass
class SearchRequest:
    keyword: str  # 不含收藏筛选
    count: int = 1
    mode: str = "random"  # random / popular / recent
    min_bookmarks: int = 0
    min_quality: float = 0.0
    allow_r18: bool = False
    exclude: set[str] = field(default_factory=set)  # 不要的作品 ID（最近发过的）
    max_age_hours: int = 0  # 只要这么多小时内发布的；0 表示不限
    max_rounds: int = MAX_ROUNDS
    detail_batch: int = DETAIL_BATCH


@dataclass
class SearchOutcome:
    images: list[dict[str, Any]]
    total: int = 0  # Pixiv 说这个关键词一共有多少作品
    reason: str = ""  # 没凑够时的原因（errors.REASONS 里的 key）
    stats: dict[str, int] = field(default_factory=dict)


def _id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "")


def search_items(body: dict[str, Any], *, include_popular: bool = True) -> tuple[list[dict[str, Any]], int, int]:
    """搜索结果 → (作品列表, 总数, 最后一页)。热门列表（popular，非会员也有）合进来，靠 ID 去重。"""
    manga = body.get("illustManga") or body.get("illust") or {}
    items = list(manga.get("data") or [])
    if include_popular:
        popular = body.get("popular") or {}
        items += list(popular.get("permanent") or []) + list(popular.get("recent") or [])
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        # 广告位是 {"isAdContainer": true}，没有 id
        if isinstance(item, dict) and _id(item) and _id(item) not in unique:
            unique[_id(item)] = item
    total = int(manga.get("total") or len(unique))
    last_page = int(manga.get("lastPage") or max(1, -(-total // PAGE_SIZE)))
    return list(unique.values()), total, last_page


def bookmark_tag(min_bookmarks: int) -> int:
    """不超过 min_bookmarks 的最大档位；不到 100 时返回 0（不加标签）。"""
    return max((t for t in USERS_TAGS if t <= min_bookmarks), default=0)


def bookmark_keyword(keyword: str, min_bookmarks: int) -> str:
    tag = bookmark_tag(min_bookmarks)
    return f"{keyword} {tag}users入り" if tag else keyword


class Searcher:
    def __init__(
        self,
        api,
        tag_filter: Callable[[], TagFilter],
        *,
        rng: random.Random | None = None,
        max_page: Callable[[], int] = lambda: DEFAULT_MAX_PAGE,
        premium: Callable[[], bool] = lambda: False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.api = api
        self._filter = tag_filter
        self.rng = rng or random.Random()
        self._max_page = max_page
        self._premium = premium
        self._clock = clock
        # (关键词, 排序) → 一共几页
        self._last_pages: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------ 翻页

    def _order(self, mode: str) -> str:
        if mode != "recent" and self._premium():
            return "popular_d"
        return "date_d"

    def _page_limit(self, last_page: int) -> int:
        return max(1, min(last_page, max(1, int(self._max_page()))))

    def _remember(self, key: tuple[str, str], last_page: int) -> None:
        self._last_pages[key] = last_page
        while len(self._last_pages) > LAST_PAGE_MEMORY:
            self._last_pages.pop(next(iter(self._last_pages)))

    async def _page(self, keyword: str, page: int, order: str, popular: bool):
        body = await self.api.search(keyword, page, order=order)
        items, total, last_page = search_items(body, include_popular=popular)
        return items, total, last_page, body.get("tagTranslation") or {}

    async def _try_page(self, keyword: str, page: int, order: str, popular: bool):
        """后面的页出错（太深、被删）时当成空页，并把记下的页数调小。"""
        try:
            return await self._page(keyword, page, order, popular)
        except PixivError as e:
            if e.reason in FATAL:
                raise
            key = (keyword, order)
            if key in self._last_pages:
                self._last_pages[key] = max(1, min(self._last_pages[key], page - 1))
            return [], 0, 0, {}

    def _pages_to_try(self, mode: str, last_page: int) -> list[int]:
        """第一页之后还不够时，接着翻哪几页。最新模式按顺序往后翻，其他模式随机跳。"""
        if mode == "recent":
            return list(range(1, min(last_page, MAX_ROUNDS * 2) + 1))
        pages = list(range(1, self._page_limit(last_page) + 1))
        self.rng.shuffle(pages)
        return pages

    async def _first_batch(self, keyword: str, order: str, mode: str, popular: bool):
        """返回 (作品, 总数, 最后一页, 翻译, 用的是第几页)。"""
        key = (keyword, order)
        known = self._last_pages.get(key)
        if mode != "recent" and known and known > 1:
            page = self.rng.randint(1, self._page_limit(known))
            items, total, last_page, translations = await self._try_page(keyword, page, order, popular)
            if items:
                self._remember(key, last_page or known)
                return items, total, last_page or known, translations, page
        items, total, last_page, translations = await self._page(keyword, 1, order, popular)
        if not items:
            self._last_pages.pop(key, None)
            return items, total, last_page, translations, 1
        self._remember(key, last_page)
        if mode != "recent" and last_page > 1:
            # 第一次搜这个词：已经知道一共几页了，马上跳到随机的一页，不然每次重启后都从最新的 60 张开始
            page = self.rng.randint(1, self._page_limit(last_page))
            if page != 1:
                more, _, _, more_translations = await self._try_page(keyword, page, order, popular)
                if more:
                    return more, total, last_page, more_translations, page
        return items, total, last_page, translations, 1

    # ------------------------------------------------------------ 详情和挑选

    async def _details(self, items: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        sem = asyncio.Semaphore(DETAIL_PARALLEL)

        async def one(item):
            async with sem:
                try:
                    return item, await self.api.illust(_id(item))
                except PixivError as e:
                    if e.reason in FATAL:
                        raise
                    return item, None

        results = await asyncio.gather(*(one(i) for i in items))
        return [(item, detail) for item, detail in results if detail]

    def _weighted_pick(self, scored: list[tuple[float, dict[str, Any]]], count: int) -> list[dict[str, Any]]:
        """按分数加权、不放回地抽 count 个。分数平方让好图明显更容易被抽到。"""
        pool = list(scored)
        picked = []
        while pool and len(picked) < count:
            weights = [max(s, 1.0) ** 2 for s, _ in pool]
            index = self.rng.choices(range(len(pool)), weights=weights)[0]
            picked.append(pool.pop(index)[1])
        return picked

    def _too_old(self, item: dict[str, Any], cutoff: datetime | None) -> bool:
        if cutoff is None:
            return False
        created = quality.parse_time(item.get("createDate") or item.get("uploadDate"))
        return created is None or created < cutoff

    # ------------------------------------------------------------ 入口

    async def search(self, req: SearchRequest) -> SearchOutcome:
        recent = req.mode == "recent"
        order = self._order(req.mode)
        tag_filter = self._filter()
        cutoff = self._clock() - timedelta(hours=req.max_age_hours) if req.max_age_hours else None
        stats = {"pages": 0, "seen": 0, "blocked": 0, "details": 0, "low_bookmarks": 0, "low_quality": 0,
                 "too_old": 0}
        use_tag = req.min_bookmarks > 0 and not recent and bookmark_tag(req.min_bookmarks) > 0
        keyword = bookmark_keyword(req.keyword, req.min_bookmarks) if use_tag else req.keyword
        popular = not recent

        items, total, last_page, translations, first = await self._first_batch(keyword, order, req.mode, popular)
        stats["pages"] += 1
        if not items and use_tag:
            # 「N users入り」是读者手动加的标签，冷门标签下很多高收藏作品没有：去掉再搜，按真实收藏数筛
            keyword = req.keyword
            items, total, last_page, translations, first = await self._first_batch(keyword, order, req.mode, popular)
            stats["pages"] += 1
        if not items:
            return SearchOutcome([], total, "no_results", stats)

        pages = [p for p in self._pages_to_try(req.mode, last_page) if p != first]
        picked: list[dict[str, Any]] = []
        fallback: list[tuple[float, dict[str, Any]]] = []  # 质量分不够、但其他条件都满足的
        checked: set[str] = set()
        rounds = 0
        while True:
            rounds += 1
            fresh = []
            page_too_old = 0
            for item in items:
                illust_id = _id(item)
                if illust_id in checked:
                    continue
                checked.add(illust_id)
                if self._too_old(item, cutoff):
                    stats["too_old"] += 1
                    page_too_old += 1
                elif illust_id in req.exclude:
                    stats["seen"] += 1
                elif (not req.allow_r18 and int(item.get("xRestrict") or 0) > 0) or tag_filter.blocks(item, translations):
                    stats["blocked"] += 1
                else:
                    fresh.append(item)
            if not recent:
                self.rng.shuffle(fresh)
            candidates = fresh[: req.detail_batch]
            stats["details"] += len(candidates)
            scored = []
            for item, detail in await self._details(candidates):
                if tag_filter.blocks(detail):  # 详情里的标签带翻译，比搜索结果全
                    stats["blocked"] += 1
                    continue
                if int(detail.get("bookmarkCount") or 0) < req.min_bookmarks:
                    stats["low_bookmarks"] += 1
                    continue
                value = quality.score(detail).total
                merged = {**item, **{k: detail.get(k) for k in ("bookmarkCount", "viewCount", "likeCount", "pageCount",
                                                                 "width", "height", "createDate", "xRestrict")},
                          "quality_score": round(value, 1)}
                if value < req.min_quality:
                    stats["low_quality"] += 1
                    fallback.append((value, merged))
                    continue
                scored.append((value, merged))
            need = req.count - len(picked)
            # 最新模式按新旧顺序（详情是并发查的，顺序要按 ID 重新排），其他模式按质量加权随机
            picked += ([m for _, m in sorted(scored, key=lambda p: int(_id(p[1])), reverse=True)][:need]
                       if recent else self._weighted_pick(scored, need))
            # 最新模式：这一页全都超出时间范围，后面的页只会更旧
            exhausted = recent and items and page_too_old == len(items)
            if len(picked) >= req.count or rounds >= req.max_rounds or not pages or exhausted:
                break
            items, _, _, translations = await self._try_page(keyword, pages.pop(0), order, popular)
            stats["pages"] += 1

        if len(picked) < req.count and fallback:
            # 质量阈值是「优先」，不是硬条件：凑不够时用分数最高的补上
            fallback.sort(key=lambda pair: pair[0], reverse=True)
            picked += [image for _, image in fallback[: req.count - len(picked)]]
        reason = ""
        if not picked:
            if stats["too_old"] and not (stats["details"] or stats["seen"]):
                reason = "no_recent_images"
            elif stats["seen"] and not (stats["details"] - stats["low_bookmarks"]):
                reason = "all_seen"
            elif stats["low_bookmarks"]:
                reason = "low_bookmarks"
            elif stats["blocked"]:
                reason = "all_blocked"
            else:
                reason = "no_qualified_images"
        return SearchOutcome(picked, total, reason, stats)
