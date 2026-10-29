"""房间状态机。纯逻辑，不依赖网络和 NoneBot，所有时间由调用方传入，方便测试。

播放状态以服务端为准：客户端发来的控制指令先在这里校验、更新状态，再广播给所有人。
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .sources.base import Track
from .sync import Anchor

MAX_QUEUE = 200
HISTORY_SIZE = 50

# 每人每 10 秒最多 3 次控制操作（播放、暂停、拖进度、切歌）。
CONTROL_WINDOW_MS = 10_000
CONTROL_LIMIT = 3


class RoomError(Exception):
    """操作被拒绝。message 直接显示给用户。"""


@dataclass
class QueueItem:
    item_id: int
    track: Track
    added_by: str  # QQ 号
    added_by_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "track": self.track.to_dict(),
            "added_by": self.added_by,
            "added_by_name": self.added_by_name,
        }


@dataclass
class Room:
    group_id: str
    owner: str
    allow_control: bool = True
    current: QueueItem | None = None
    queue: list[QueueItem] = field(default_factory=list)
    history: deque[Track] = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))
    anchor: Anchor = field(default_factory=lambda: Anchor(0, 0, paused=True))
    version: int = 0
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1), repr=False)
    _controls: dict[str, deque[int]] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------ 查询

    def position_at(self, now: int) -> int:
        return self.anchor.position_at(now)

    def finished(self, now: int) -> bool:
        """当前这首放完了（暂停时永远不算放完）。"""
        if self.current is None or self.anchor.paused:
            return False
        duration = self.current.track.duration_ms
        return duration > 0 and self.position_at(now) >= duration

    def remaining_ms(self, now: int) -> int | None:
        if self.current is None or self.anchor.paused or self.current.track.duration_ms <= 0:
            return None
        return max(0, self.current.track.duration_ms - self.position_at(now))

    def state(self, now: int) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "owner": self.owner,
            "allow_control": self.allow_control,
            "current": self.current.to_dict() if self.current else None,
            "paused": self.anchor.paused,
            "anchor_server_ms": self.anchor.server_ms,
            "anchor_position_ms": self.anchor.position_ms,
            "server_ms": now,
            "version": self.version,
        }

    def queue_dict(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.queue]

    # ------------------------------------------------------------ 权限

    def can_control(self, qq: str, *, superuser: bool = False) -> bool:
        return self.allow_control or superuser or qq == self.owner

    def _check_control(self, qq: str, now: int, *, superuser: bool) -> None:
        if not self.can_control(qq, superuser=superuser):
            raise RoomError("房主关掉了大家控制播放的权限")
        if superuser or qq == self.owner:
            return
        recent = self._controls.setdefault(qq, deque())
        while recent and now - recent[0] >= CONTROL_WINDOW_MS:
            recent.popleft()
        if len(recent) >= CONTROL_LIMIT:
            raise RoomError("操作太频繁了，稍等几秒")
        recent.append(now)

    def _check_version(self, version: int | None) -> None:
        # 客户端看到的状态已经过期（别人刚操作过），丢弃这次操作，让客户端用最新状态。
        if version is not None and version != self.version:
            raise RoomError("播放状态刚被别人改过，已同步到最新")

    def _bump(self, anchor: Anchor) -> None:
        self.anchor = anchor
        self.version += 1

    # ------------------------------------------------------------ 播放控制

    def play(self, qq: str, now: int, *, version: int | None = None, superuser: bool = False) -> None:
        self._check_version(version)
        if self.current is None:
            if not self.queue:
                raise RoomError("队列是空的，先点首歌吧")
            self._check_control(qq, now, superuser=superuser)
            self._advance(now)
            return
        if not self.anchor.paused:
            return
        self._check_control(qq, now, superuser=superuser)
        self._bump(Anchor(now, self.anchor.position_ms, paused=False))

    def pause(self, qq: str, now: int, *, version: int | None = None, superuser: bool = False) -> None:
        self._check_version(version)
        if self.current is None or self.anchor.paused:
            return
        self._check_control(qq, now, superuser=superuser)
        self._bump(Anchor(now, self.position_at(now), paused=True))

    def seek(
        self, qq: str, now: int, position_ms: int, *, version: int | None = None, superuser: bool = False
    ) -> None:
        self._check_version(version)
        if self.current is None:
            raise RoomError("现在没有在放歌")
        self._check_control(qq, now, superuser=superuser)
        duration = self.current.track.duration_ms
        position_ms = max(0, int(position_ms))
        if duration > 0:
            position_ms = min(position_ms, max(0, duration - 1000))
        self._bump(Anchor(now, position_ms, paused=self.anchor.paused))

    def next(self, qq: str, now: int, *, version: int | None = None, superuser: bool = False) -> None:
        self._check_version(version)
        if self.current is None and not self.queue:
            raise RoomError("后面没有歌了")
        self._check_control(qq, now, superuser=superuser)
        self._advance(now)

    def replace_current(self, track: Track, now: int) -> None:
        """当前这首在原平台放不了，换成别的平台的同一首，从头放。保留点歌人。"""
        if self.current is None:
            return
        self.current = QueueItem(next(self._ids), track, self.current.added_by, self.current.added_by_name)
        self._bump(Anchor(now, 0, paused=self.anchor.paused))

    def skip_current(self, now: int) -> None:
        """当前这首放不了时由服务端跳过，不检查权限，不记入播放历史。"""
        self.current = None
        self._advance(now)

    def auto_advance(self, now: int) -> bool:
        """服务端计时器调用：当前这首放完了就切下一首。返回是否切了。"""
        if not self.finished(now):
            return False
        self._advance(now)
        return True

    def _advance(self, now: int) -> None:
        if self.current is not None:
            self.history.append(self.current.track)
        if self.queue:
            self.current = self.queue.pop(0)
            self._bump(Anchor(now, 0, paused=False))
        else:
            self.current = None
            self._bump(Anchor(now, 0, paused=True))

    # ------------------------------------------------------------ 队列

    def add(self, track: Track, qq: str, name: str) -> QueueItem:
        if len(self.queue) >= MAX_QUEUE:
            raise RoomError(f"队列最多 {MAX_QUEUE} 首")
        item = QueueItem(next(self._ids), track, qq, name)
        self.queue.append(item)
        return item

    def remove(self, item_id: int, qq: str, *, superuser: bool = False) -> None:
        item = self._find(item_id)
        if item.added_by != qq and not self.can_control(qq, superuser=superuser):
            raise RoomError("只能删自己点的歌")
        self.queue.remove(item)

    def move(self, item_id: int, to: int, qq: str, *, superuser: bool = False) -> None:
        item = self._find(item_id)
        if not self.can_control(qq, superuser=superuser):
            raise RoomError("房主关掉了大家调整队列的权限")
        self.queue.remove(item)
        self.queue.insert(max(0, min(int(to), len(self.queue))), item)

    def _find(self, item_id: int) -> QueueItem:
        for item in self.queue:
            if item.item_id == item_id:
                return item
        raise RoomError("这首歌已经不在队列里了")

    # ------------------------------------------------------------ 房主

    def set_allow_control(self, value: bool, qq: str, *, superuser: bool = False) -> None:
        if qq != self.owner and not superuser:
            raise RoomError("只有房主能改这个")
        self.allow_control = bool(value)
        self.version += 1

    # ------------------------------------------------------------ 保存与恢复

    def snapshot(self, now: int) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "owner": self.owner,
            "allow_control": self.allow_control,
            "current": self.current.to_dict() if self.current else None,
            "position_ms": self.position_at(now),
            "queue": self.queue_dict(),
            "history": [t.to_dict() for t in self.history],
        }

    @classmethod
    def restore(cls, data: dict[str, Any], now: int) -> Room:
        """bot 重启后恢复。恢复成暂停状态，等有人点播放。"""
        room = cls(group_id=str(data["group_id"]), owner=str(data["owner"]))
        room.allow_control = bool(data.get("allow_control", True))
        items = []
        for raw in [data.get("current"), *(data.get("queue") or [])]:
            if raw:
                items.append(QueueItem(next(room._ids), Track.from_dict(raw["track"]), raw["added_by"],
                                       raw.get("added_by_name", "")))
        if data.get("current") and items:
            room.current = items.pop(0)
            room.anchor = Anchor(now, int(data.get("position_ms") or 0), paused=True)
        room.queue = items
        room.history.extend(Track.from_dict(t) for t in data.get("history") or [])
        return room
