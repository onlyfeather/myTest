"""B 站：搜视频、读视频信息和字幕。不依赖 NoneBot，方便单独测试。

接口参考社区整理的 bilibili-API-collect，B 站随时可能改：
- 搜索 /x/web-interface/wbi/search/type：要 WBI 签名和 buvid3 Cookie，不够会返回 -412
- 视频信息 /x/web-interface/view
- 字幕 /x/player/wbi/v2：要登录（SESSDATA）才有

只请求 B 站自己的域名：短链只认 b23.tv，跳转目标必须是 bilibili.com；封面和字幕只从 *.hdslb.com 下载。
"""

from __future__ import annotations

import hashlib
import html
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .http import ServiceError, describe_http_error, http_client

NAME = "B站"
API = "https://api.bilibili.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
# WBI 签名用的字符重排表（来自 B 站前端代码，只用前 32 位）
MIXIN_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)
KEY_TTL = 3600
SHORT_HOSTS = ("b23.tv", "bili2233.cn")
MEDIA_SUFFIX = ".hdslb.com"
MAX_SUBTITLE = 3000
MAX_COVER = 5 * 1024 * 1024
ORDERS = {"relevant": "totalrank", "popular": "click", "latest": "pubdate"}

_BV = re.compile(r"BV1[0-9A-Za-z]{9}")
_AV = re.compile(r"(?<![0-9A-Za-z])av(\d{1,12})(?!\d)", re.IGNORECASE)
_PAGE = re.compile(r"[?&]p=(\d{1,4})")
_SHORT = re.compile(r"https?://(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]{4,16}")
_TAG = re.compile(r"<[^>]+>")

# 进程内缓存：WBI 密钥每天换，buvid3 是匿名访客标识
_keys: tuple[float, str, str] | None = None
_buvid: dict[str, str] = {}


# ---------------------------------------------------------------- 签名、解析（纯函数）


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_TABLE if i < len(raw))[:32]


def wbi_query(params: dict[str, Any]) -> str:
    """按键名排序、去掉 !'() 后编码，和浏览器的 encodeURIComponent 一样（空格是 %20）。"""
    clean = {k: "".join(c for c in str(v) if c not in "!'()") for k, v in params.items()}
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='*')}" for k, v in sorted(clean.items()))


def sign(params: dict[str, Any], img_key: str, sub_key: str, *, now: float | None = None) -> dict[str, str]:
    signed = {k: "".join(c for c in str(v) if c not in "!'()") for k, v in params.items()}
    signed["wts"] = str(int(time.time() if now is None else now))
    query = wbi_query(signed)
    signed["w_rid"] = hashlib.md5((query + mixin_key(img_key, sub_key)).encode()).hexdigest()
    return signed


def parse_cookie(text: str) -> dict[str, str]:
    """WebUI 里填的 Cookie：只填 SESSDATA 的值，或者整串「k=v; k=v」都行。"""
    text = str(text or "").strip()
    if not text:
        return {}
    if "=" not in text:
        return {"SESSDATA": text}
    out = {}
    for part in text.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


@dataclass(frozen=True)
class VideoRef:
    bvid: str = ""
    aid: int = 0
    page: int = 1


def parse_ref(text: str) -> VideoRef | None:
    """从文字里认出视频：BV 号、av 号、bilibili.com/video/... 链接。短链要先 resolve。"""
    text = str(text or "")
    page = int(m[1]) if (m := _PAGE.search(text)) else 1
    page = max(1, page)
    if m := _BV.search(text):
        return VideoRef(bvid=m[0], page=page)
    if m := _AV.search(text):
        return VideoRef(aid=int(m[1]), page=page)
    return None


def find_short_link(text: str) -> str:
    m = _SHORT.search(str(text or ""))
    return m[0] if m else ""


def _host_ok(url: str, *, suffix: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == suffix.lstrip(".") or host.endswith(suffix))


def media_url(url: str) -> str:
    """封面、字幕地址：补上 https:，只允许 *.hdslb.com。不合格返回空字符串。"""
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if _host_ok(url, suffix=MEDIA_SUFFIX) else ""


def clean_text(text: Any) -> str:
    """搜索结果的标题带 <em class="keyword"> 高亮和 HTML 转义。"""
    return html.unescape(_TAG.sub("", str(text or ""))).strip()


def count_text(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n or "")
    return f"{n / 10000:.1f}万".replace(".0万", "万") if n >= 10000 else str(n)


def duration_text(value: Any) -> str:
    """搜索结果是「4:18」，视频信息是秒数。"""
    if isinstance(value, int | float):
        seconds = int(value)
        h, rest = divmod(seconds, 3600)
        m, s = divmod(rest, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return str(value or "")


@dataclass
class Video:
    bvid: str
    title: str
    author: str = ""
    desc: str = ""
    duration: str = ""
    play: str = ""
    cover: str = ""
    aid: int = 0
    cid: int = 0

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"


def video_from_search(item: dict[str, Any]) -> Video | None:
    bvid = str(item.get("bvid") or "")
    if not _BV.fullmatch(bvid):
        return None
    return Video(
        bvid=bvid,
        title=clean_text(item.get("title")),
        author=clean_text(item.get("author")),
        desc=clean_text(item.get("description")),
        duration=duration_text(item.get("duration")),
        play=count_text(item.get("play")),
        cover=media_url(str(item.get("pic") or "")),
        aid=int(item.get("aid") or 0),
    )


def video_from_view(data: dict[str, Any], page: int = 1) -> Video:
    pages = data.get("pages") or []
    cid = int(data.get("cid") or 0)
    duration = data.get("duration")
    if 1 < page <= len(pages):
        cid = int(pages[page - 1].get("cid") or cid)
        duration = pages[page - 1].get("duration", duration)
    return Video(
        bvid=str(data.get("bvid") or ""),
        title=clean_text(data.get("title")),
        author=clean_text((data.get("owner") or {}).get("name")),
        desc=clean_text(data.get("desc")),
        duration=duration_text(duration),
        play=count_text((data.get("stat") or {}).get("view")),
        cover=media_url(str(data.get("pic") or "")),
        aid=int(data.get("aid") or 0),
        cid=cid,
    )


def pick_subtitle(subtitles: list[dict[str, Any]]) -> str:
    """优先中文字幕（人工的在前，AI 的 ai-zh 也算）。返回字幕文件地址。"""
    ranked = sorted(subtitles, key=lambda s: (not str(s.get("lan") or "").startswith("zh"),
                                              "zh" not in str(s.get("lan") or "")))
    for sub in ranked:
        if url := media_url(str(sub.get("subtitle_url") or "")):
            return url
    return ""


def subtitle_text(data: dict[str, Any], limit: int = MAX_SUBTITLE) -> str:
    lines = [str(line.get("content") or "").strip() for line in data.get("body") or [] if isinstance(line, dict)]
    text = "，".join(line for line in lines if line)
    return text if len(text) <= limit else text[:limit] + "……（后面还有）"


ERRORS = {
    -101: "没有登录",
    -400: "请求参数不对",
    -403: "没有权限",
    -404: "视频不存在或已经删除",
    -412: "请求被 B 站拦截了，过一会儿再试，或者在 WebUI「B站」里填登录 Cookie",
    62002: "视频不可见",
    62004: "视频审核中",
}


# ---------------------------------------------------------------- 接口


def _set_cookies(client: httpx.AsyncClient, cookies: dict[str, str]) -> None:
    """只发给 bilibili.com，不发给封面、字幕所在的 hdslb.com 和短链 b23.tv。"""
    for key, value in cookies.items():
        client.cookies.set(key, value, domain=".bilibili.com")


class Bilibili:
    def __init__(self, cookie: str = "", proxy: str = "", timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cookie = parse_cookie(cookie)
        self.proxy = proxy
        self.timeout = timeout
        self.transport = transport

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self.transport is not None:  # 测试
            async with httpx.AsyncClient(transport=self.transport, follow_redirects=True) as client:
                client.headers.update(HEADERS)
                yield client
            return
        async with http_client(self.proxy, self.timeout) as client:
            client.headers.update(HEADERS)
            yield client

    async def _json(self, client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None, *,
                    allow: tuple[int, ...] = ()) -> Any:
        resp = await client.get(url, params=params)
        if resp.is_error:
            raise ServiceError(describe_http_error(NAME, resp))
        try:
            data = resp.json()
        except ValueError:
            raise ServiceError(f"{NAME} 返回的不是 JSON（HTTP {resp.status_code}）") from None
        code = data.get("code", 0) if isinstance(data, dict) else 0
        if code and code not in allow:
            hint = ERRORS.get(code) or str(data.get("message") or "")
            raise ServiceError(f"{NAME} 返回错误 {code}：{hint}")
        return data.get("data") if isinstance(data, dict) and "code" in data else data

    async def _prepare(self, client: httpx.AsyncClient) -> None:
        """带上登录 Cookie；没有 buvid3 时先领一个匿名访客标识（搜索要它）。"""
        _set_cookies(client, self.cookie)
        if "buvid3" in self.cookie:
            return
        if not _buvid:
            data = await self._json(client, f"{API}/x/frontend/finger/spi")
            if isinstance(data, dict) and data.get("b_3"):
                _buvid.update(buvid3=str(data["b_3"]), buvid4=str(data.get("b_4") or ""))
        _set_cookies(client, {k: v for k, v in _buvid.items() if v})

    async def _wbi_keys(self, client: httpx.AsyncClient) -> tuple[str, str]:
        global _keys
        if _keys and time.monotonic() - _keys[0] < KEY_TTL:
            return _keys[1], _keys[2]
        data = await self._json(client, f"{API}/x/web-interface/nav", allow=(-101,))
        img = (data or {}).get("wbi_img") or {}

        def stem(url: str) -> str:
            return str(url or "").rsplit("/", 1)[-1].split(".")[0]

        img_key, sub_key = stem(img.get("img_url")), stem(img.get("sub_url"))
        if len(img_key + sub_key) < 64:
            raise ServiceError(f"{NAME} 签名密钥取不到，接口可能改了")
        _keys = (time.monotonic(), img_key, sub_key)
        return img_key, sub_key

    async def _signed(self, client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
        img_key, sub_key = await self._wbi_keys(client)
        return await self._json(client, f"{API}{path}", sign(params, img_key, sub_key))

    async def search(self, keyword: str, *, order: str = "totalrank") -> list[Video]:
        async with self._client() as client:
            await self._prepare(client)
            data = await self._signed(client, "/x/web-interface/wbi/search/type", {
                "search_type": "video", "keyword": keyword, "order": order, "page": 1,
            })
        items = (data or {}).get("result") or []
        return [v for item in items if isinstance(item, dict) and (v := video_from_search(item))]

    async def resolve(self, text: str) -> VideoRef:
        """链接、BV 号、av 号、b23.tv 短链 → 视频。认不出来抛 ServiceError。"""
        if ref := parse_ref(text):
            return ref
        short = find_short_link(text)
        if not short:
            raise ServiceError("没认出 B 站视频链接（要 BV 号、av 号、bilibili.com 或 b23.tv 链接）")
        async with self._client() as client:
            resp = await client.get(short, follow_redirects=False)
        target = resp.headers.get("location", "")
        host = (urlsplit(target).hostname or "").lower() if target else ""
        if not (host == "bilibili.com" or host.endswith(".bilibili.com")):
            raise ServiceError("这个短链接没有跳到 B 站视频")
        if ref := parse_ref(target):
            return ref
        raise ServiceError("这个短链接不是视频（可能是直播、动态或专栏）")

    async def video(self, ref: VideoRef) -> Video:
        params = {"bvid": ref.bvid} if ref.bvid else {"aid": ref.aid}
        async with self._client() as client:
            await self._prepare(client)
            data = await self._json(client, f"{API}/x/web-interface/view", params)
        return video_from_view(data or {}, ref.page)

    async def subtitle(self, video: Video) -> str:
        """视频的字幕文字；没有字幕或没登录拿不到时返回空字符串。"""
        if not video.cid:
            return ""
        async with self._client() as client:
            await self._prepare(client)
            data = await self._signed(client, "/x/player/wbi/v2", {"bvid": video.bvid, "cid": video.cid})
            url = pick_subtitle(((data or {}).get("subtitle") or {}).get("subtitles") or [])
            if not url:
                return ""
            resp = await client.get(url, follow_redirects=False)
        if resp.is_error:
            return ""
        try:
            return subtitle_text(resp.json())
        except ValueError:
            return ""

    async def cover(self, video: Video) -> bytes:
        url = media_url(video.cover)
        if not url:
            return b""
        async with self._client() as client:
            resp = await client.get(url, follow_redirects=False)
        if resp.is_error or len(resp.content) > MAX_COVER:
            return b""
        return resp.content

    async def login_name(self) -> str:
        """登录的账号名；没登录返回空字符串。"""
        async with self._client() as client:
            _set_cookies(client, self.cookie)
            data = await self._json(client, f"{API}/x/web-interface/nav", allow=(-101,))
        return str((data or {}).get("uname") or "") if (data or {}).get("isLogin") else ""
