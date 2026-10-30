"""Pixiv 功能的入口：把请求（pixiv/api.py）、本地数据（library.py）、搜图（search.py）、质量评分（quality.py）组合起来。

原来 1900 行的 PixivSpider 拆成了上面几块；这里保留原来的方法名，handlers / services 不用跟着改。
请求失败统一抛 PixivError（说明直接给用户看）；只有「查一下有没有」的方法（get_user_info 之类）
在失败时返回 None，并把原因记在 last_error_reason 里。
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from nonebot import logger

from . import quality
from .config import config
from .daily import DailyPicker
from .library import Library, normalize_alias
from .pixiv.api import PixivAPI
from .pixiv.errors import PixivError
from .pixiv_storage import PixivStorage
from .search import SearchRequest, Searcher
from .tag_filter import tag_names

# 失败原因按请求（asyncio 任务）隔离，避免并发请求互相覆盖。
_last_error_reason: ContextVar[Optional[str]] = ContextVar("pixiv_last_error_reason", default=None)


class PixivSpider:
    """进程内只应存在一个实例，由 pixiv.client 管理。"""

    def __init__(
        self,
        storage: Optional[PixivStorage] = None,
        cookie_source: Optional[Callable[[], str]] = None,
        api: Optional[PixivAPI] = None,
    ):
        self.storage = storage or PixivStorage()
        self.api = api or PixivAPI(cookie_source or (lambda: ""), image_host=lambda: config.image_proxy_host)
        self.library = Library(self.storage)
        self.searcher = self.make_searcher()
        self.daily = DailyPicker(self.storage, self.make_searcher, lambda: self.library.tags.preferred,
                                 lambda: self.library.min_quality_score)

    def make_searcher(self, rng=None) -> Searcher:
        from .config import pixiv_settings

        return Searcher(
            self.api,
            lambda: self.library.tags.filter,
            rng=rng,
            max_page=lambda: int(pixiv_settings.max_page),
            premium=lambda: bool(pixiv_settings.premium),
        )

    async def start(self) -> None:
        self.library.load()
        await self.api.start()
        if not self.is_logged_in:
            self.last_error_reason = "missing_cookie"

    async def close(self) -> None:
        await self.api.close()

    # ------------------------------------------------------------ 状态

    @property
    def last_error_reason(self) -> Optional[str]:
        return _last_error_reason.get()

    @last_error_reason.setter
    def last_error_reason(self, value: Optional[str]) -> None:
        _last_error_reason.set(value)

    @property
    def full_cookie(self) -> Optional[str]:
        return self.api.cookie

    @property
    def is_logged_in(self) -> bool:
        return self.api.logged_in

    @property
    def user_id(self) -> Optional[str]:
        return self.api.user_id

    @user_id.setter
    def user_id(self, value: Optional[str]) -> None:
        self.api.account_id = str(value) if value else None

    async def check_login(self) -> str:
        return await self.api.check_login()

    async def _try(self, coro) -> Any:
        """查询类方法用：失败时记下原因、返回 None。"""
        try:
            result = await coro
        except PixivError as e:
            self.last_error_reason = e.reason
            logger.info("[Pixiv] 请求失败：{}（{}）", e.reason, e.detail[:100])
            return None
        return result

    # ------------------------------------------------------------ 本地数据（转给 library）

    @property
    def min_quality_score(self) -> float:
        return self.library.min_quality_score

    def set_min_quality_score(self, score: int) -> int:
        return self.library.set_min_quality_score(score)

    @property
    def preferred_tags(self) -> set:
        return self.library.tags.preferred

    @property
    def blocked_tags(self) -> set:
        return self.library.tags.blocked

    def add_preferred_tag(self, tag: str) -> bool:
        return self.library.tags.add("preferred", tag)

    def remove_preferred_tag(self, tag: str) -> bool:
        return self.library.tags.remove("preferred", tag)

    def add_blocked_tag(self, tag: str) -> bool:
        return self.library.tags.add("blocked", tag)

    def remove_blocked_tag(self, tag: str) -> bool:
        return self.library.tags.remove("blocked", tag)

    def get_tag_pools_info(self) -> Dict[str, Any]:
        preferred, blocked = sorted(self.preferred_tags), sorted(self.blocked_tags)
        return {"preferred_tags": preferred, "blocked_tags": blocked,
                "preferred_count": len(preferred), "blocked_count": len(blocked)}

    def is_blocked(self, illust: Dict[str, Any], *, allow_r18: bool = True) -> bool:
        if not allow_r18 and int(illust.get("xRestrict") or 0) > 0:
            return True
        return self.library.tags.filter.blocks(illust)

    @property
    def favorite_authors(self) -> Dict[str, Dict[str, Any]]:
        return self.library.authors.authors

    def add_favorite_author(self, user_id: str, user_name: str = "") -> bool:
        return self.library.authors.add_many([(user_id, user_name)]) == 1

    def add_favorite_authors_bulk(self, authors: List[tuple]) -> int:
        return self.library.authors.add_many(authors)

    def remove_favorite_author(self, user_id: str) -> bool:
        return self.library.authors.remove(user_id)

    def get_favorite_author_info(self, user_id: str) -> Optional[Dict[str, Any]]:
        return self.library.authors.get(user_id)

    def get_favorite_authors_info(self) -> Dict[str, Any]:
        return self.library.authors.summary()

    def update_author_last_check(self, user_id: str) -> None:
        self.library.authors.touch(user_id)

    def search_author_by_alias(self, alias: str) -> Optional[str]:
        return self.library.authors.resolve(alias)

    def get_all_aliases(self) -> Dict[str, str]:
        return self.library.authors.aliases()

    def set_author_aliases(self, user_id: str, aliases: List[str]) -> Optional[str]:
        return self.library.authors.set_aliases(user_id, aliases)

    def add_author_alias(self, user_id: str, alias: str) -> bool:
        return self.library.authors.add_alias(user_id, alias) is None

    def remove_author_alias(self, user_id: str, alias: str) -> bool:
        owner = self.library.authors.alias_owner(alias)
        return owner == str(user_id) and self.library.authors.remove_alias(alias) is None

    @staticmethod
    def _normalize_alias(alias: str) -> str:
        return normalize_alias(alias)

    # ------------------------------------------------------------ 搜图

    async def search(self, req: SearchRequest):
        """新的搜图入口，失败抛 PixivError。"""
        return await self.searcher.search(req)

    async def search_illustrations(self, tag: str, page: int = 1, order: str = "date_d") -> Optional[Dict[str, Any]]:
        """一页搜索结果，屏蔽标签已经过滤掉。给每日一图、美图这些还没改到新搜图的功能用。"""
        from .search import search_items

        body = await self._try(self.api.search(str(tag), page, order=order))
        if body is None:
            return None
        items, total, last_page = search_items(body)
        translations = body.get("tagTranslation") or {}
        tag_filter = self.library.tags.filter
        kept = [self._simplify(i) for i in items if not tag_filter.blocks(i, translations)]
        self.last_error_reason = None
        return {"illusts": kept, "total": total, "lastPage": last_page,
                "popular": {"recent": [], "permanent": []}, "tagTranslation": translations}

    async def search_latest_illustrations(self, keyword: str, page: int = 1) -> Optional[Dict[str, Any]]:
        return await self.search_illustrations(keyword, page, order="date_d")

    def _simplify(self, illust: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("id", "title", "tags", "userId", "userName", "pageCount", "width", "height", "illustType",
                "xRestrict", "description", "createDate", "aiType", "profileImageUrl")
        result = {key: illust.get(key) for key in keys}
        result["url"] = self.api.proxy_url(illust.get("url") or "")
        result["tags"] = tag_names(illust.get("tags"))
        return result

    # ------------------------------------------------------------ 作品

    async def get_illust_details(self, illust_id: str) -> Optional[Dict[str, Any]]:
        return await self._try(self.api.illust(str(illust_id)))

    async def get_illust_original_urls(self, illust_id: str) -> Optional[List[str]]:
        return await self._try(self.api.original_urls(str(illust_id)))

    def calculate_quality_score(self, details: Dict[str, Any]) -> Dict[str, Any]:
        return quality.score(details).as_dict()

    def parse_pixiv_time(self, time_str: str) -> Optional[datetime]:
        return quality.parse_time(time_str)

    async def get_image_info(self, illust_id: str) -> Optional[Dict[str, Any]]:
        details = await self.get_illust_details(illust_id)
        if not details:
            return None
        urls = await self.get_illust_original_urls(str(details.get("id") or illust_id)) or []
        info = {key: details.get(key) for key in (
            "id", "title", "userId", "userName", "pageCount", "width", "height", "illustType", "xRestrict",
            "description", "createDate", "aiType", "bookmarkCount", "likeCount", "viewCount", "commentCount")}
        info.update({"urls": urls or [self.api.proxy_url(details.get("url") or "")],
                     "tags": [t for t in tag_names(details.get("tags")) if t], "source": "image_info_api",
                     "quality_score": round(quality.score(details).total, 1)})
        # 详情里的 tags 带翻译，tag_names 会把翻译也展开；显示时只要原标签
        raw_tags = (details.get("tags") or {}).get("tags") if isinstance(details.get("tags"), dict) else None
        if raw_tags:
            info["tags"] = [t.get("tag") for t in raw_tags if isinstance(t, dict) and t.get("tag")]
        return info

    # ------------------------------------------------------------ 作者

    async def get_user_info(self, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """返回 {"error": False, "body": {...}}，和原来的格式一样。"""
        body = await self._try(self.api.user(user_id))
        return {"error": False, "body": body} if body is not None else None

    async def get_author_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        return await self._try(self.api.user_profile(str(user_id)))

    async def get_following(self, user_id: str, *, offset: int = 0, limit: int = 100,
                            rest: str = "show") -> Optional[Dict[str, Any]]:
        return await self._try(self.api.following(str(user_id), offset=offset, limit=limit, rest=rest))

    async def get_author_info(self, user_id: str) -> Optional[Dict[str, Any]]:
        user_id = str(user_id or "").strip()
        if not user_id:
            return None
        user = await self._try(self.api.user(user_id))
        if user is None:
            return None
        profile = await self.get_author_profile(user_id) or {}
        raw = profile.get("illusts") or {}
        illusts = sorted(raw.values() if isinstance(raw, dict) else [], key=lambda i: int(i.get("id") or 0),
                         reverse=True)
        latest = [{"id": i.get("id"), "title": i.get("title", ""), "createDate": i.get("createDate", ""),
                   "pageCount": i.get("pageCount", 1), "tags": tag_names(i.get("tags")), "urls": []}
                  for i in illusts[:5]]
        return {
            "userId": user.get("userId", user_id),
            "name": user.get("name", ""),
            "comment": user.get("comment", ""),
            "image": self.api.proxy_url(user.get("image") or ""),
            "imageBig": self.api.proxy_url(user.get("imageBig") or ""),
            "isFollowed": user.get("isFollowed", False),
            "isMypixiv": user.get("isMypixiv", False),
            "isPremium": user.get("premium", user.get("isPremium", False)),
            "totalWorks": len(raw) if isinstance(raw, dict) else 0,
            "followable": profile.get("followable", user.get("followable", False)),
            "acceptRequest": user.get("acceptRequest", False),
            "latestImages": latest,
        }


