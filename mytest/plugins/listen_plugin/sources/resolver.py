"""多个音源的组合：熔断、在别的平台找同一首歌。"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from .base import (
    PLATFORM_NAMES,
    MusicSource,
    SourceError,
    SourceUnavailable,
    Stream,
    Track,
)

FAIL_LIMIT = 5
COOLDOWN = 300.0
DURATION_TOLERANCE_MS = 3000

# 歌名里括号中的版本说明（Live、伴奏、DJ 版……）。比较“是不是同一首”时，括号外的部分必须一致，括号内也必须一致：
# 晴天 和 晴天 (Live) 不算同一首。
_PUNCT = re.compile(r"[\s\-_·・,，.。!！?？'\"“”‘’/／&＆+]+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.replace("（", "(").replace("）", ")").replace("【", "[").replace("】", "]")
    return _PUNCT.sub("", text)


def artist_set(text: str) -> set[str]:
    parts = re.split(r"[、,，/&＆;；]| feat\.? | ft\.? ", unicodedata.normalize("NFKC", text or "").lower())
    return {normalize(p) for p in parts if normalize(p)}


_BAND_VERSION = re.compile(r"\(([^()]*)\s*ver\.?\)$", re.IGNORECASE)


def _title_key(title: str, artists: str) -> str:
    """去掉“(歌手名 ver.)”这种只是重复歌手名的后缀：酷狗写「春日影」，QQ 音乐写「春日影 (MyGO!!!!! ver.)」。"""
    text = unicodedata.normalize("NFKC", title or "").strip()
    m = _BAND_VERSION.search(text.replace("（", "(").replace("）", ")"))
    if m and normalize(m.group(1)) in artist_set(artists):
        text = text[: m.start()]
    return normalize(text)


def same_song(a: Track, b: Track) -> bool:
    if _title_key(a.title, a.artists) != _title_key(b.title, b.artists):
        return False
    if a.duration_ms and b.duration_ms and abs(a.duration_ms - b.duration_ms) > DURATION_TOLERANCE_MS:
        return False
    return bool(artist_set(a.artists) & artist_set(b.artists))


@dataclass
class _Health:
    failures: int = 0
    open_until: float = 0.0


class Sources:
    """按平台管理音源。连续失败 5 次的平台停用 5 分钟，期间直接报错，不拖慢搜索。"""

    def __init__(self, sources: dict[str, MusicSource], *, clock: Callable[[], float] = time.monotonic):
        self._sources = sources
        self._health = {name: _Health() for name in sources}
        self._clock = clock

    def __contains__(self, platform: str) -> bool:
        return platform in self._sources

    def platforms(self) -> list[str]:
        return list(self._sources)

    def get(self, platform: str) -> MusicSource | None:
        return self._sources.get(platform)

    def values(self) -> list[MusicSource]:
        return list(self._sources.values())

    def available(self, platform: str) -> bool:
        health = self._health.get(platform)
        return health is not None and self._clock() >= health.open_until

    def _ok(self, platform: str) -> None:
        self._health[platform] = _Health()

    def _fail(self, platform: str) -> None:
        health = self._health[platform]
        health.failures += 1
        if health.failures >= FAIL_LIMIT:
            health.open_until = self._clock() + COOLDOWN
            health.failures = 0

    def _source(self, platform: str) -> MusicSource:
        source = self._sources.get(platform)
        if source is None:
            raise SourceError(f"{PLATFORM_NAMES.get(platform, platform)} 没有接入")
        if not self.available(platform):
            raise SourceError(f"{PLATFORM_NAMES.get(platform, platform)} 暂时不可用，几分钟后再试")
        return source

    async def call(self, platform: str, name: str, *args: object, timeout: float = 8.0) -> object:
        """调用某个平台的方法。连接失败、超时算故障；“这首没有地址”这类正常的业务结果不算。"""
        source = self._source(platform)
        try:
            result = await asyncio.wait_for(getattr(source, name)(*args), timeout)
        except TimeoutError:
            self._fail(platform)
            raise SourceError(f"{PLATFORM_NAMES.get(platform, platform)} 响应超时") from None
        except SourceUnavailable:
            self._fail(platform)
            raise
        except SourceError:
            raise
        self._ok(platform)
        return result

    async def search(self, platform: str, keyword: str, limit: int = 20) -> list[Track]:
        result = await self.call(platform, "search", keyword, limit)
        return result  # type: ignore[return-value]

    async def stream(self, track: Track, bitrate: int) -> Stream:
        result = await self.call(track.platform, "stream", track, bitrate, timeout=10.0)
        return result  # type: ignore[return-value]

    async def lyrics(self, track: Track) -> tuple[str, str]:
        result = await self.call(track.platform, "lyrics", track)
        return result  # type: ignore[return-value]

    async def find_elsewhere(self, track: Track, *, vip_ready: Callable[[str], bool]) -> list[Track]:
        """在别的平台找同一首歌（歌名一致、歌手有交集、时长差 3 秒以内）。只返回能放的（免费或者那边配了会员）。"""
        keyword = f"{track.title} {track.artists.split('、')[0]}".strip()
        others = [p for p in self._sources if p != track.platform and self.available(p)]

        async def one(platform: str) -> list[Track]:
            try:
                found = await self.search(platform, keyword, 10)
            except SourceError:
                return []
            return [t for t in found if same_song(track, t) and (not t.vip or vip_ready(platform))]

        results = await asyncio.gather(*(one(p) for p in others))
        return [t for group in results for t in group]
