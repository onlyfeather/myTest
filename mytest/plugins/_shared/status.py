"""运行状态：各插件登记自己的状态（平台熔断、账号到期、推送情况），WebUI「运行状态」页汇总显示。

另外在内存里记最近的警告、错误和工具调用，排查问题不用进容器看日志。
本模块不依赖 NoneBot，方便单独测试；把日志接进来的 install_log_sink 只在 NoneBot 里调用。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

LEVELS = ("ok", "warn", "error", "")
MAX_TEXT = 300


@dataclass(frozen=True)
class StatusRow:
    label: str
    value: str
    level: str = ""  # ok / warn / error / 空（只是信息）

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "value": self.value, "level": self.level if self.level in LEVELS else ""}


Provider = Callable[[], list[StatusRow]]


@dataclass
class _Section:
    id: str
    title: str
    provider: Provider
    order: int


class StatusRegistry:
    def __init__(self) -> None:
        self._sections: dict[str, _Section] = {}

    def register(self, section_id: str, title: str, provider: Provider, *, order: int = 100) -> None:
        self._sections[section_id] = _Section(section_id, title, provider, order)

    def snapshot(self) -> list[dict[str, Any]]:
        result = []
        for section in sorted(self._sections.values(), key=lambda s: (s.order, s.id)):
            try:
                rows = [row.to_dict() for row in section.provider()]
                error = ""
            except Exception as e:  # noqa: BLE001 一个插件的状态读不出来不影响整页
                rows, error = [], f"{type(e).__name__}: {str(e)[:200]}"
            result.append({"id": section.id, "title": section.title, "rows": rows, "error": error})
        return result


@dataclass(frozen=True)
class Event:
    time: float
    channel: str  # log / tools
    level: str  # ok / warn / error
    source: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"time": self.time, "channel": self.channel, "level": self.level,
                "source": self.source, "message": self.message}


class EventLog:
    """每个频道各保留最近 limit 条。"""

    def __init__(self, limit: int = 100, clock: Callable[[], float] = time.time) -> None:
        self._limit = limit
        self._clock = clock
        self._channels: dict[str, deque[Event]] = {}
        self._lock = threading.Lock()

    def record(self, channel: str, level: str, source: str, message: str) -> None:
        event = Event(self._clock(), channel, level, source[:60], " ".join(str(message).split())[:MAX_TEXT])
        with self._lock:
            self._channels.setdefault(channel, deque(maxlen=self._limit)).append(event)

    def recent(self, channel: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._channels.get(channel, ()))
        return [e.to_dict() for e in reversed(items[-limit:])]


status_registry = StatusRegistry()
event_log = EventLog()
STARTED_AT = time.time()


def ago(timestamp: float, now: float | None = None) -> str:
    """多久以前，例如「3 分钟前」。timestamp 为 0 时返回「从未」。"""
    if not timestamp:
        return "从未"
    seconds = max(0, int((now or time.time()) - timestamp))
    if seconds < 60:
        return "刚刚"
    for size, unit in ((86400, "天"), (3600, "小时"), (60, "分钟")):
        if seconds >= size:
            return f"{seconds // size} {unit}前"
    return "刚刚"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def full_snapshot() -> dict[str, Any]:
    return {
        "sections": status_registry.snapshot(),
        "logs": event_log.recent("log"),
        "tools": event_log.recent("tools"),
        "now": time.time(),
    }


_sink_installed = False


def install_log_sink() -> None:
    """把 loguru 的 WARNING 及以上记到事件里。只装一次。"""
    global _sink_installed
    if _sink_installed:
        return
    from nonebot import logger

    def sink(message: Any) -> None:
        record = message.record
        level = "error" if record["level"].no >= 40 else "warn"
        text = record["message"]
        if record["exception"] is not None and record["exception"].type is not None:
            text += f"（{record['exception'].type.__name__}: {record['exception'].value}）"
        event_log.record("log", level, record["name"] or "", text)

    logger.add(sink, level="WARNING", format="{message}")
    _sink_installed = True
