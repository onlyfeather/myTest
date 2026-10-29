"""一起听的业务核心：把登录、群成员检查、房间、音源组合起来。HTTP 层（app.py）和群指令（__init__.py）都调用这里。

不依赖 NoneBot：和 bot 有关的操作都经过 BotBridge。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .auth import AuthError, CodeBook, Identity, LinkSigner, Sessions
from .bridge import BotBridge, Member, MembershipCache
from .hub import Hub
from .sources.base import MusicSource, SourceError, Stream, Track
from .store import Store
from .sync import now_ms

MAX_TRACK_CACHE = 5000


@dataclass
class ListenConfig:
    public_url: str
    secret: str
    allowed_groups: Callable[[], list[str]]
    trusted_proxies: tuple[str, ...] = ("127.0.0.1", "::1")
    max_members: Callable[[], int] = lambda: 10
    bitrate: Callable[[], int] = lambda: 320
    downgrade_over: Callable[[], int] = lambda: 10

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")


@dataclass
class _CachedStream:
    stream: Stream
    expires: float


@dataclass
class ListenService:
    config: ListenConfig
    bridge: BotBridge
    store: Store
    sources: dict[str, MusicSource]
    clock: Callable[[], int] = now_ms
    signer: LinkSigner = field(init=False)
    sessions: Sessions = field(init=False)
    codes: CodeBook = field(init=False)
    members: MembershipCache = field(init=False)
    hub: Hub = field(init=False)

    def __post_init__(self) -> None:
        self.signer = LinkSigner(self.config.secret)
        self.sessions = Sessions(self.store)
        self.codes = CodeBook()
        self.members = MembershipCache(self.bridge)
        self.hub = Hub(self.store, clock=self.clock)
        # 搜索结果按 ref 缓存。点歌只接受搜索过的 ref，歌名等信息以服务端为准，客户端改不了。
        self._tracks: OrderedDict[str, Track] = OrderedDict()
        self._streams: dict[tuple[str, int], _CachedStream] = {}

    # ------------------------------------------------------------ 白名单和成员

    def group_allowed(self, group_id: str | None) -> bool:
        return bool(group_id) and group_id in set(self.config.allowed_groups())

    async def check_member(self, identity: Identity) -> Member | None:
        """白名单里、而且还在群里才算数。bot 不在线时抛 BotUnavailable，由调用方决定怎么提示。"""
        if not self.group_allowed(identity.group_id):
            return None
        return await self.members.member(identity.group_id, identity.qq)

    async def groups_of(self, qq: str) -> list[str]:
        """这个人所在的白名单群。"""
        found = []
        for group_id in self.config.allowed_groups():
            if await self.members.member(group_id, qq):
                found.append(group_id)
        return found

    def is_superuser(self, qq: str) -> bool:
        return self.bridge.is_superuser(qq)

    async def member_left(self, group_id: str, qq: str) -> None:
        self.members.forget(group_id, qq)
        self.sessions.revoke_user(qq, group_id)
        await self.hub.kick(group_id, qq, "你已经不在群里了")

    async def enforce_whitelist(self) -> None:
        """群被移出白名单：关房间，作废这个群的所有登录。"""
        allowed = list(self.config.allowed_groups())
        for group_id in self.hub.groups():
            if group_id not in allowed:
                await self.hub.close_room(group_id, "这个群的一起听已被关闭")
        self.store.delete_sessions_outside(allowed)

    # ------------------------------------------------------------ 登录

    def login_url(self, qq: str, group_id: str) -> str:
        token = self.signer.issue(qq, group_id, self.clock())
        return f"{self.config.public_url.rstrip('/')}/login#t={token}"

    def redeem_code(self, code: str, qq: str, group_id: str) -> bool:
        return self.codes.redeem(code, Identity(qq, group_id), self.clock())

    async def login_with_link(self, token: str) -> tuple[str, Member]:
        identity = self.sessions.consume_link(self.signer, token, self.clock())
        member = await self.check_member(identity)
        if member is None:
            raise AuthError("你不在这个群里，或者这个群没有开放一起听")
        return self.sessions.create(identity, self.clock()), member

    # ------------------------------------------------------------ 歌曲

    def remember(self, tracks: list[Track]) -> None:
        for track in tracks:
            self._tracks[track.ref] = track
            self._tracks.move_to_end(track.ref)
        while len(self._tracks) > MAX_TRACK_CACHE:
            self._tracks.popitem(last=False)

    def known_track(self, ref: str) -> Track | None:
        return self._tracks.get(ref)

    async def search(self, keyword: str, platform: str = "qq", limit: int = 20) -> list[Track]:
        source = self.sources.get(platform)
        if source is None:
            raise SourceError("这个平台还没接入")
        tracks = await source.search(keyword, limit)
        self.remember(tracks)
        return tracks

    def vip_ready(self, platform: str) -> bool:
        """这个平台配了会员账号（M3 接入），会员歌才放得出来。"""
        return bool(getattr(self.sources.get(platform), "vip_ready", False))

    def bitrate_for(self, group_id: str) -> int:
        """在线人数超过阈值时降到 128kbps，省带宽。"""
        if self.hub.online(group_id) > self.config.downgrade_over():
            return 128
        return 320 if self.config.bitrate() >= 320 else 128

    async def stream(self, track: Track, bitrate: int) -> Stream:
        key = (track.ref, bitrate)
        cached = self._streams.get(key)
        now = time.monotonic()
        if cached and now < cached.expires:
            return cached.stream
        source = self.sources.get(track.platform)
        if source is None:
            raise SourceError("这个平台还没接入")
        stream = await source.stream(track, bitrate)
        # 提前 5 分钟当作过期，避免放到一半地址失效。
        self._streams[key] = _CachedStream(stream, now + max(60, stream.expires_in - 300))
        if len(self._streams) > 2000:
            self._streams = {k: v for k, v in self._streams.items() if v.expires > now}
        return stream
