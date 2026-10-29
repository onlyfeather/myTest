"""一起听服务和 bot 之间的接口。服务端只依赖这里的 BotBridge 协议，不 import nonebot。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class BotUnavailable(Exception):
    """bot 不在线，暂时查不了群成员。"""


@dataclass(frozen=True)
class Member:
    qq: str
    group_id: str
    nickname: str
    card: str = ""
    title: str = ""
    level: str = ""
    role: str = "member"  # owner / admin / member

    @property
    def name(self) -> str:
        return self.card or self.nickname or self.qq


class BotBridge(Protocol):
    async def member(self, group_id: str, qq: str) -> Member | None:
        """查群成员。不在群里返回 None；bot 不在线抛 BotUnavailable。"""
        ...

    def is_superuser(self, qq: str) -> bool: ...


class MembershipCache:
    """群成员查询结果缓存：在群 10 分钟，不在群 1 分钟。退群通知到达时调用 forget 立即失效。"""

    def __init__(
        self,
        bridge: BotBridge,
        *,
        ttl: float = 600,
        negative_ttl: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._bridge = bridge
        self._ttl = ttl
        self._negative_ttl = negative_ttl
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, Member | None]] = {}

    async def member(self, group_id: str, qq: str) -> Member | None:
        key = (group_id, qq)
        hit = self._cache.get(key)
        now = self._clock()
        if hit and now < hit[0]:
            return hit[1]
        found = await self._bridge.member(group_id, qq)
        self._cache[key] = (now + (self._ttl if found else self._negative_ttl), found)
        if len(self._cache) > 5000:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        return found

    def forget(self, group_id: str, qq: str | None = None) -> None:
        if qq is None:
            self._cache = {k: v for k, v in self._cache.items() if k[0] != group_id}
        else:
            self._cache.pop((group_id, qq), None)

    def is_superuser(self, qq: str) -> bool:
        return self._bridge.is_superuser(qq)
