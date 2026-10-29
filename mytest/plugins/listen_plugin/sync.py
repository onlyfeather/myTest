"""播放进度的计算。纯函数，服务端和测试共用；前端 app.js 里有同样的逻辑。"""

from __future__ import annotations

import time
from dataclasses import dataclass


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Anchor:
    """最近一次状态变化：那一刻（服务端时间）播放到哪里、是否暂停。"""

    server_ms: int
    position_ms: int
    paused: bool

    def position_at(self, server_ms: int) -> int:
        if self.paused:
            return self.position_ms
        return self.position_ms + max(0, server_ms - self.server_ms)


# 纠偏阈值（毫秒）：小于 IGNORE 不处理，小于 JUMP 用变速追平，再大就直接跳。
IGNORE_MS = 300
JUMP_MS = 2000


def correction(local_ms: int, expected_ms: int) -> tuple[str, float]:
    """返回 ("none" | "rate" | "seek", playbackRate)。本地落后时加速，超前时减速。"""
    drift = local_ms - expected_ms
    if abs(drift) < IGNORE_MS:
        return "none", 1.0
    if abs(drift) < JUMP_MS:
        return "rate", 0.95 if drift > 0 else 1.05
    return "seek", 1.0
