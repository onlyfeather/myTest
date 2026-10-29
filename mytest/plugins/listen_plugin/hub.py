"""房间管理：每个群一个房间、在线连接、广播、自动切歌、空房间关闭。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .bridge import Member
from .room import Room
from .store import Store
from .sync import now_ms

# 最后一个人离开 30 分钟后关闭房间（队列保留在数据库里，下次开房恢复）。
IDLE_CLOSE_MS = 30 * 60 * 1000


@dataclass(eq=False)
class Connection:
    member: Member
    superuser: bool
    send: Callable[[dict[str, Any]], Awaitable[None]]
    close: Callable[[int, str], Awaitable[None]]


@dataclass(eq=False)
class LiveRoom:
    room: Room
    conns: set[Connection] = field(default_factory=set)
    empty_since: int | None = None
    timer: asyncio.Task | None = None


class Hub:
    def __init__(self, store: Store, *, clock: Callable[[], int] = now_ms):
        self._store = store
        self._clock = clock
        self._rooms: dict[str, LiveRoom] = {}
        self._reaper: asyncio.Task | None = None

    # ------------------------------------------------------------ 房间

    def get(self, group_id: str) -> Room | None:
        live = self._rooms.get(group_id)
        return live.room if live else None

    def open(self, group_id: str, owner: str) -> tuple[Room, bool]:
        """开房。已经开着就返回现有房间。返回 (房间, 是否新开)。"""
        live = self._rooms.get(group_id)
        if live:
            return live.room, False
        saved = self._store.load_room(group_id)
        now = self._clock()
        room = Room.restore(saved, now) if saved else Room(group_id=group_id, owner=owner)
        room.owner = owner
        self._rooms[group_id] = LiveRoom(room, empty_since=now)
        self.save(group_id)
        return room, True

    async def close_room(self, group_id: str, reason: str = "一起听已关闭") -> None:
        live = self._rooms.pop(group_id, None)
        if not live:
            return
        self._store.save_room(group_id, live.room.snapshot(self._clock()), self._clock())
        if live.timer:
            live.timer.cancel()
        for conn in list(live.conns):
            with contextlib.suppress(Exception):
                await conn.send({"type": "closed", "message": reason})
                await conn.close(4000, "closed")

    def save(self, group_id: str) -> None:
        live = self._rooms.get(group_id)
        if live:
            now = self._clock()
            self._store.save_room(group_id, live.room.snapshot(now), now)

    def groups(self) -> list[str]:
        return list(self._rooms)

    # ------------------------------------------------------------ 连接

    def join(self, group_id: str, conn: Connection) -> Room | None:
        live = self._rooms.get(group_id)
        if not live:
            return None
        live.conns.add(conn)
        live.empty_since = None
        return live.room

    def leave(self, group_id: str, conn: Connection) -> None:
        live = self._rooms.get(group_id)
        if not live:
            return
        live.conns.discard(conn)
        if not live.conns:
            live.empty_since = self._clock()

    def online(self, group_id: str) -> int:
        live = self._rooms.get(group_id)
        return len({c.member.qq for c in live.conns}) if live else 0

    def members(self, group_id: str) -> list[dict[str, Any]]:
        live = self._rooms.get(group_id)
        if not live:
            return []
        seen: dict[str, dict[str, Any]] = {}
        for c in live.conns:
            seen.setdefault(
                c.member.qq,
                {"qq": c.member.qq, "name": c.member.name, "title": c.member.title, "role": c.member.role},
            )
        return list(seen.values())

    async def kick(self, group_id: str, qq: str, reason: str) -> None:
        live = self._rooms.get(group_id)
        if not live:
            return
        for conn in [c for c in live.conns if c.member.qq == qq]:
            live.conns.discard(conn)
            with contextlib.suppress(Exception):
                await conn.send({"type": "closed", "message": reason})
                await conn.close(4003, "kicked")
        await self.broadcast(group_id, {"type": "members", "members": self.members(group_id)})

    async def broadcast(self, group_id: str, message: dict[str, Any]) -> None:
        live = self._rooms.get(group_id)
        if not live:
            return
        dead = []
        for conn in list(live.conns):
            try:
                await conn.send(message)
            except Exception:  # noqa: BLE001 连接已经断了，下面统一清理
                dead.append(conn)
        for conn in dead:
            live.conns.discard(conn)

    async def push_state(self, group_id: str) -> None:
        room = self.get(group_id)
        if room:
            await self.broadcast(group_id, {"type": "state", "state": room.state(self._clock())})
            self.reschedule(group_id)

    async def push_queue(self, group_id: str) -> None:
        room = self.get(group_id)
        if room:
            await self.broadcast(group_id, {"type": "queue", "queue": room.queue_dict()})

    # ------------------------------------------------------------ 自动切歌

    def reschedule(self, group_id: str) -> None:
        """当前这首放完时由服务端切下一首，不依赖客户端上报（多人同时上报会连跳）。"""
        live = self._rooms.get(group_id)
        if not live:
            return
        if live.timer:
            live.timer.cancel()
            live.timer = None
        remaining = live.room.remaining_ms(self._clock())
        if remaining is None:
            return
        live.timer = asyncio.get_running_loop().create_task(self._advance_later(group_id, remaining))

    async def _advance_later(self, group_id: str, delay_ms: int) -> None:
        await asyncio.sleep(delay_ms / 1000 + 0.5)
        room = self.get(group_id)
        if room and room.auto_advance(self._clock()):
            self.save(group_id)
            await self.push_queue(group_id)
            await self.push_state(group_id)

    # ------------------------------------------------------------ 空房间

    def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            self._reaper = None
        for group_id in list(self._rooms):
            await self.close_room(group_id, "服务器重启，稍后再进")

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.reap()

    async def reap(self) -> None:
        now = self._clock()
        for group_id, live in list(self._rooms.items()):
            if not live.conns and live.empty_since is not None and now - live.empty_since >= IDLE_CLOSE_MS:
                await self.close_room(group_id, "没人在听，一起听已关闭")
        self._store.purge(now)
