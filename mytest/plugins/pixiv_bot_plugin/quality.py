"""作品质量评分（0-100）。纯计算，不依赖 NoneBot。

旧办法把收藏、评论、点赞直接加权相加，再按「是不是 72 小时内」切成两套标准，
结果是刚过 72 小时的作品分数断崖下跌、浏览少的作品靠运气拿高分。现在用三个互相独立、各自稳定的信号：

1. 收藏率（收藏 / 浏览）：最能说明「看过的人喜不喜欢」，和作品新旧、作者粉丝多少关系不大。
   Pixiv 上一般作品在 1%-3%，好作品 8% 以上，15% 以上就很少见了。
   浏览少时收藏率不可靠（10 浏览 3 收藏 = 30%），用贝叶斯平均往全站常见值收缩：
   相当于先假设它已经有 PRIOR_VIEWS 次浏览、收藏率是 PRIOR_RATE。
2. 热度（每天新增收藏，取对数）：同样的收藏率，十万收藏比一百收藏更有分量；按天折算后新作品不吃亏。
   取对数是因为热度跨好几个数量级，线性会让少数爆款把其他全挤到零分。
3. 可信度：上面两项都需要样本。浏览不到几百时打折，避免冷门作品偶然冲到榜首。

总分 = 可信度 × (0.6 × 收藏率分 + 0.4 × 热度分) + (1 - 可信度) × 中性分。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PRIOR_RATE = 0.03  # 全站常见收藏率
PRIOR_VIEWS = 300  # 收缩强度：浏览远多于这个数时基本只看自己的数据
EXCELLENT_RATE = 0.15  # 收藏率达到这个值就算满分
DAILY_FULL = 2000  # 每天新增这么多收藏算热度满分
CONFIDENT_VIEWS = 2000  # 浏览到这个数时可信度接近 1
NEUTRAL = 40.0  # 数据不够时向这个分数靠
RATE_WEIGHT = 0.6
MIN_AGE_DAYS = 0.25  # 刚发 6 小时以内按 6 小时算，避免按天折算时被放大
OLD_AGE_DAYS = 90  # 老作品的热度按 90 天封顶折算，不让十年前的作品因为天数太多被压到零


@dataclass(frozen=True)
class QualityScore:
    total: float
    rate_score: float
    popularity_score: float
    confidence: float
    bookmark_rate: float  # 收缩后的收藏率

    @property
    def level(self) -> str:
        for threshold, name in ((85, "S"), (70, "A"), (55, "B"), (40, "C")):
            if self.total >= threshold:
                return name
        return "D"

    def as_dict(self) -> dict[str, Any]:
        """兼容旧代码读 total_score。"""
        return {
            "total_score": round(self.total, 1),
            "rate_score": round(self.rate_score, 1),
            "popularity_score": round(self.popularity_score, 1),
            "confidence": round(self.confidence, 2),
            "bookmark_rate": round(self.bookmark_rate, 4),
            "quality_level": self.level,
        }


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _int(details: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(details.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def score(details: dict[str, Any], now: datetime | None = None) -> QualityScore:
    """details 是 /ajax/illust/{id} 的 body（bookmarkCount、viewCount、createDate）。"""
    bookmarks = _int(details, "bookmarkCount")
    views = _int(details, "viewCount")
    # 浏览数偶尔比收藏少（统计延迟），按收藏数兜底
    views = max(views, bookmarks)

    rate = (bookmarks + PRIOR_RATE * PRIOR_VIEWS) / (views + PRIOR_VIEWS)
    rate_score = 100 * min(1.0, math.log1p(rate / PRIOR_RATE) / math.log1p(EXCELLENT_RATE / PRIOR_RATE))

    created = parse_time(details.get("createDate") or details.get("uploadDate"))
    now = now or datetime.now(timezone.utc)
    age_days = (now - created).total_seconds() / 86400 if created else 30.0
    age_days = min(max(age_days, MIN_AGE_DAYS), OLD_AGE_DAYS)
    per_day = bookmarks / age_days
    popularity_score = 100 * min(1.0, math.log1p(per_day) / math.log1p(DAILY_FULL))

    confidence = 1 - math.exp(-views / (CONFIDENT_VIEWS / 3))
    raw = RATE_WEIGHT * rate_score + (1 - RATE_WEIGHT) * popularity_score
    total = confidence * raw + (1 - confidence) * NEUTRAL
    return QualityScore(total, rate_score, popularity_score, confidence, rate)
