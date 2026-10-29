"""点歌的业务核心：三个平台搜歌、挑出要发的那首、取卡片里的播放地址。群指令和凛音的工具都调用这里。

不依赖 NoneBot：发消息由调用方做。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .sources.base import PLATFORMS, MusicSource, SourceError, Stream, Track, VipCredential
from .sources.resolver import Sources, same_song
from .vip import VipAccounts

MAX_TRACK_CACHE = 2000
_VERSION_WORDS = re.compile(r"live|伴奏|instrumental|karaoke|カラオケ|翻自|cover|dj|remix|0\.\d+x", re.IGNORECASE)


@dataclass
class PlatformResult:
    """一个平台的搜索结果。error 不为空时表示这个平台这次没搜成（没接入、超时、熔断中）。"""

    platform: str
    tracks: list[Track] = field(default_factory=list)
    error: str = ""


@dataclass
class _CachedStream:
    stream: Stream
    expires: float


class MusicService:
    def __init__(self, source_map: dict[str, MusicSource], vip: VipAccounts | None = None):
        self.source_map = source_map
        self.sources = Sources(source_map)
        self.vip = vip
        self._tracks: OrderedDict[str, Track] = OrderedDict()
        self._streams: dict[str, _CachedStream] = {}
        if vip is not None:
            self._bind_vip(vip)

    def _bind_vip(self, vip: VipAccounts) -> None:
        """把 bot 的会员账号凭证交给各音源。登录、失效时清掉已缓存的播放地址。"""
        for platform, source in self.source_map.items():
            if isinstance(source, VipCredential):
                source.bind_credential(
                    lambda p=platform: vip.credential(p),
                    lambda message, p=platform: vip.credential_failed(p, message),
                )

        def changed(platform: str) -> None:
            self._streams = {k: v for k, v in self._streams.items() if not k.startswith(f"{platform}:")}

        vip.on_changed = changed

    # ------------------------------------------------------------ 平台

    def platforms(self) -> list[str]:
        """按固定顺序（QQ 音乐、网易云、酷狗）列出接入了的平台。"""
        return [p for p in PLATFORMS if p in self.sources]

    def vip_ready(self, platform: str) -> bool:
        return bool(getattr(self.sources.get(platform), "vip_ready", False))

    def playable(self, track: Track) -> bool:
        return not track.vip or self.vip_ready(track.platform)

    def remember(self, tracks: list[Track]) -> None:
        for track in tracks:
            self._tracks[track.ref] = track
            self._tracks.move_to_end(track.ref)
        while len(self._tracks) > MAX_TRACK_CACHE:
            self._tracks.popitem(last=False)

    def known_track(self, ref: str) -> Track | None:
        return self._tracks.get(ref)

    # ------------------------------------------------------------ 搜索

    async def search_all(self, keyword: str, per_platform: int = 3) -> list[PlatformResult]:
        """三个平台一起搜，各自出错不影响别的平台。返回顺序固定，没接入的平台不出现。"""

        async def one(platform: str) -> PlatformResult:
            try:
                tracks = await self.sources.search(platform, keyword, per_platform)
            except SourceError as e:
                return PlatformResult(platform, error=str(e))
            tracks = tracks[:per_platform]
            self.remember(tracks)
            return PlatformResult(platform, tracks)

        return list(await asyncio.gather(*(one(p) for p in self.platforms())))

    async def resolve_song(self, keyword: str) -> Track | None:
        """凛音发歌时按关键词找一首。

        先认定“是哪首歌”：各平台搜索结果里排第一的原版（不是 Live / 伴奏 / 翻唱，除非关键词里写了）。
        再找这首歌能放的那份：同一首（歌名、歌手、时长对得上）里优先能放的。
        不会为了能放就换成别人翻唱的同名歌：宁可发一张放不了、但点开能跳到原曲的卡片。
        """
        candidates: list[Track] = []
        for result in await self.search_all(keyword, 5):
            candidates.extend(result.tracks)
        if not candidates:
            return None
        asked = _VERSION_WORDS.search(keyword)
        if asked:
            # 关键词里写了 Live、伴奏之类：要的就是那个版本
            word = asked.group(0).lower()
            preferred = [t for t in candidates if word in t.title.lower()]
        else:
            preferred = [t for t in candidates if not _VERSION_WORDS.search(t.title)]
        target = (preferred or candidates)[0]
        copies = [t for t in candidates if t == target or same_song(target, t)]
        return next((t for t in copies if self.playable(t)), target)

    # ------------------------------------------------------------ 卡片

    async def stream(self, track: Track, bitrate: int = 320) -> Stream:
        cached = self._streams.get(track.ref)
        now = time.monotonic()
        if cached and now < cached.expires:
            return cached.stream
        stream = await self.sources.stream(track, bitrate)
        # 提前 5 分钟当作过期，避免卡片里的地址刚发出去就失效。
        self._streams[track.ref] = _CachedStream(stream, now + max(60, stream.expires_in - 300))
        if len(self._streams) > 1000:
            self._streams = {k: v for k, v in self._streams.items() if v.expires > now}
        return stream

    async def card_audio(self, track: Track) -> str:
        """卡片里点播放时放的地址。网易云免费歌用官方外链（长期有效）；其他取现在的直链。取不到返回空，卡片照发。"""
        if track.platform == "netease" and not track.vip:
            return f"https://music.163.com/song/media/outer/url?id={track.id}.mp3"
        try:
            return (await self.stream(track)).url
        except SourceError:
            return ""
