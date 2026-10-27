"""决定一条群消息要不要让她开口。

规则（按优先级）：
1. 必须回复：@ 她、回复她的消息、消息里叫了她的名字或别名。
2. 可以回复：命中 topic=true 的世界书 / 群梗词条。还要满足：本群开启了话题接话、
   该词条在本群不在冷却中、本群这一小时的主动接话次数没超上限。最后由模型决定说不说。
3. 其他情况：只记进上下文，不回复。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum

from ..persona.lore import LoreEntry, keyword_hit


class TriggerKind(str, Enum):
    NONE = "none"
    DIRECT = "direct"  # 必须回复
    TOPIC = "topic"  # 可以回复，由模型决定


@dataclass
class TriggerDecision:
    kind: TriggerKind
    reason: str = ""
    topic_entry: LoreEntry | None = None


def mentions_role(text: str, aliases: list[str]) -> bool:
    return any(keyword_hit(text, alias) for alias in aliases if alias)


class TopicLimiter:
    """话题接话的冷却和每小时上限，按群统计，只在内存中（重启清零）。"""

    def __init__(self, cooldown_seconds: int, per_hour: int):
        self.cooldown_seconds = cooldown_seconds
        self.per_hour = per_hour
        self._entry_last: dict[tuple[str, str], float] = {}
        self._group_history: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, group_id: str, entry_id: str, now: float | None = None) -> bool:
        now = now or time.time()
        last = self._entry_last.get((group_id, entry_id))
        if last is not None and now - last < self.cooldown_seconds:
            return False
        history = self._group_history[group_id]
        while history and now - history[0] > 3600:
            history.popleft()
        return len(history) < self.per_hour

    def mark_checked(self, group_id: str, entry_id: str, now: float | None = None) -> None:
        """词条被拿去问过模型就进入冷却，不论模型最后说没说话，避免反复刷同一个梗。"""
        self._entry_last[(group_id, entry_id)] = now or time.time()

    def mark_spoken(self, group_id: str, now: float | None = None) -> None:
        self._group_history[group_id].append(now or time.time())


def decide(
    *,
    text: str,
    at_me: bool,
    reply_to_me: bool,
    role_aliases: list[str],
    topic_entries: list[LoreEntry],
    topic_reply_enabled: bool,
    limiter: TopicLimiter,
    group_id: str,
    now: float | None = None,
) -> TriggerDecision:
    if at_me:
        return TriggerDecision(TriggerKind.DIRECT, "at")
    if reply_to_me:
        return TriggerDecision(TriggerKind.DIRECT, "reply")
    if mentions_role(text, role_aliases):
        return TriggerDecision(TriggerKind.DIRECT, "name")

    if not topic_reply_enabled:
        return TriggerDecision(TriggerKind.NONE)
    for entry in topic_entries:
        if entry.topic and limiter.allowed(group_id, entry.entry_id, now):
            return TriggerDecision(TriggerKind.TOPIC, f"topic:{entry.entry_id}", entry)
    return TriggerDecision(TriggerKind.NONE)
