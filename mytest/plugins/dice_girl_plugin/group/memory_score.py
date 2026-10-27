"""群事件的打分：保留价值（决定归档谁）和召回分（决定这次想起谁）。"""

from __future__ import annotations

import math

from ..persona.lore import keyword_hit
from ..storage.group_repository import GroupEvent

DAY = 86400.0
# 重要性 1 的事件约 15 天权重减半，重要性 5 的约 75 天。
HALF_LIFE_DAYS_PER_IMPORTANCE = 15.0


def freshness(event: GroupEvent, now: float) -> float:
    """0~1，随时间衰减；被想起过会从最近一次想起开始重新计时。"""
    reference = max(event.created_at, event.last_recalled_at or 0.0)
    age_days = max(0.0, now - reference) / DAY
    half_life = HALF_LIFE_DAYS_PER_IMPORTANCE * max(1, event.importance)
    return 0.5 ** (age_days / half_life)


def retention_score(event: GroupEvent, now: float) -> float:
    reinforcement = math.log1p(event.recall_count) * 0.3
    return event.importance * freshness(event, now) + reinforcement


def keyword_score(event: GroupEvent, text: str, mentioned_names: list[str]) -> float:
    """0~1。关键词命中、参与者被提到都算。"""
    if not text:
        return 0.0
    hits = sum(1 for kw in event.keywords if len(kw) >= 2 and keyword_hit(text, kw))
    people = sum(1 for name in event.participants if name in mentioned_names)
    return min(1.0, hits * 0.4 + people * 0.3)


def recall_score(
    event: GroupEvent,
    *,
    now: float,
    similarity: float | None,
    keyword: float,
) -> float:
    """召回分。有向量时以语义相似度为主，没有时只看关键词。"""
    relevance = keyword if similarity is None else max(similarity, keyword * 0.9)
    importance = event.importance / 5.0
    return relevance * 0.7 + importance * 0.15 + freshness(event, now) * 0.15
