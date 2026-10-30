"""Pixiv 网页版 ajax 接口的客户端：只管发请求、限流、把失败变成 PixivError。不依赖 NoneBot。

不认识的作品、作者返回 PixivError("not_found")，由上层决定是跳过还是告诉用户。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from ..cookie import cookie_user_id, normalize_cookie
from .errors import PixivError

BASE_URL = "https://www.pixiv.net"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
    "Referer": "https://www.pixiv.net/",
}
MIN_INTERVAL = 0.5  # 两次请求开始之间至少隔多久
MAX_PARALLEL = 3
URL_CACHE_SIZE = 1000


def _reason_for_status(status: int) -> str:
    if status in (401, 403):
        return "cookie_expired"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "pixiv_server_error"
    return f"http_{status}"


class PixivAPI:
    def __init__(
        self,
        cookie_source: Callable[[], str],
        *,
        image_host: Callable[[], str] = lambda: "",
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = MIN_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cookie_source = cookie_source
        self._image_host = image_host
        self._transport = transport
        self._min_interval = min_interval
        self._clock = clock
        self._client: httpx.AsyncClient | None = None
        self._slot_lock = asyncio.Lock()
        self._next_slot = 0.0
        self._parallel = asyncio.Semaphore(MAX_PARALLEL)
        self._url_cache: OrderedDict[str, list[str]] = OrderedDict()
        self.account_id: str | None = None  # Cookie 里认不出账号时，check_login 查到的

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
                transport=self._transport,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------ Cookie

    @property
    def cookie(self) -> str | None:
        return normalize_cookie(self._cookie_source()) or None

    @property
    def logged_in(self) -> bool:
        return self.cookie is not None

    @property
    def user_id(self) -> str | None:
        cookie = self.cookie
        if cookie is None:
            return None
        return cookie_user_id(cookie) or self.account_id

    # ------------------------------------------------------------ 请求

    async def _wait_slot(self) -> None:
        async with self._slot_lock:
            now = self._clock()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._min_interval
        if wait > 0:
            await asyncio.sleep(wait)

    async def _send(self, path: str, params: dict[str, Any] | None, *, auth: bool) -> httpx.Response:
        if self._client is None:
            raise PixivError("not_started")
        cookie = self.cookie
        if auth and cookie is None:
            raise PixivError("missing_cookie")
        headers = {"Cookie": cookie} if cookie else {}
        await self._wait_slot()
        async with self._parallel:
            try:
                return await self._client.get(f"{BASE_URL}{path}", params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProxyError) as e:
                raise PixivError("network_error", f"{type(e).__name__}: {e}") from e

    async def get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = True) -> Any:
        """GET /ajax/...，返回 body。"""
        response = await self._send(path, {"lang": "zh", **(params or {})}, auth=auth)
        try:
            data = response.json()
        except ValueError:
            if response.status_code != 200:
                raise PixivError(_reason_for_status(response.status_code)) from None
            raise PixivError("bad_response", response.text[:200]) from None
        message = str(data.get("message") or "") if isinstance(data, dict) else ""
        if response.status_code != 200:
            raise PixivError(_reason_for_status(response.status_code), message)
        if not isinstance(data, dict) or data.get("error"):
            # Pixiv 对不存在的作品有时也返回 200 + error
            raise PixivError("not_found" if message else "bad_response", message or str(data)[:200])
        return data.get("body")

    # ------------------------------------------------------------ 接口

    async def search(
        self, keyword: str, page: int = 1, *, order: str = "date_d", mode: str = "all", s_mode: str = "s_tag"
    ) -> dict[str, Any]:
        keyword = " ".join(keyword.split())
        if not keyword:
            raise PixivError("empty_keyword")
        body = await self.get(
            f"/ajax/search/artworks/{quote(keyword, safe='')}",
            {"word": keyword, "order": order, "mode": mode, "p": page, "s_mode": s_mode, "type": "all",
             "csw": "0", "ai_type": "1"},
        )
        return body if isinstance(body, dict) else {}

    async def illust(self, illust_id: str) -> dict[str, Any]:
        return await self.get(f"/ajax/illust/{int(illust_id)}")

    async def illust_pages(self, illust_id: str) -> list[dict[str, Any]]:
        body = await self.get(f"/ajax/illust/{int(illust_id)}/pages")
        return body if isinstance(body, list) else []

    async def user(self, user_id: str | None = None) -> dict[str, Any]:
        path = f"/ajax/user/{int(user_id)}" if user_id else "/ajax/user/self"
        return await self.get(path, {"full": 1})

    async def user_profile(self, user_id: str) -> dict[str, Any]:
        return await self.get(f"/ajax/user/{int(user_id)}/profile/top", {"sensitiveFilterMode": "userSetting"})

    async def following(self, user_id: str, *, offset: int = 0, limit: int = 100, rest: str = "show") -> dict[str, Any]:
        return await self.get(f"/ajax/user/{int(user_id)}/following",
                              {"offset": offset, "limit": limit, "rest": rest})

    async def original_urls(self, illust_id: str) -> list[str]:
        """每一页的原图地址（已换成反代域名）。"""
        key = str(illust_id)
        if key in self._url_cache:
            self._url_cache.move_to_end(key)
            return list(self._url_cache[key])
        urls = [self.proxy_url(p["urls"]["original"]) for p in await self.illust_pages(key)
                if isinstance(p, dict) and (p.get("urls") or {}).get("original")]
        self._url_cache[key] = urls
        while len(self._url_cache) > URL_CACHE_SIZE:
            self._url_cache.popitem(last=False)
        return list(urls)

    def proxy_url(self, url: str) -> str:
        host = str(self._image_host() or "").strip()
        if not url or not host:
            return url
        return url.replace("i.pximg.net", host)

    async def check_login(self) -> str:
        """测试连接：Cookie 有效时返回账号说明，无效时抛 PixivError。"""
        if not self.logged_in:
            raise PixivError("missing_cookie")
        try:
            await self.get("/ajax/user/extra")
        except PixivError as e:
            if e.reason in ("not_found", "bad_response", "http_400"):
                raise PixivError("cookie_expired", e.detail) from None
            raise
        user_id = self.user_id
        if not user_id:
            # 粘的整串 Cookie 里没有 PHPSESSID 时，从网页的响应头里认出账号
            home = await self._send("/", None, auth=True)
            self.account_id = home.headers.get("x-userid") or None
            user_id = self.account_id
        name = ""
        if user_id:
            try:
                name = str((await self.user(user_id)).get("name") or "")
            except PixivError:
                name = ""
        who = f"{name}（ID {user_id}）" if name else f"ID {user_id}" if user_id else "（认不出账号 ID）"
        return f"Cookie 有效，登录的账号：{who}"
