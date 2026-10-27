"""冷却和并发限制。画图、语音这类耗时又花钱的功能共用。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class UsageLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last_used: dict[str, float] = {}
        self._running = 0
        self._lock = asyncio.Lock()

    def cooldown_left(self, user_id: str, cooldown: float) -> int:
        last = self._last_used.get(user_id)
        if last is None or cooldown <= 0:
            return 0
        return max(0, int(last + cooldown - self._clock() + 0.999))

    async def try_acquire(self, user_id: str, *, cooldown: float, max_concurrent: int, exempt: bool) -> str:
        """成功返回空字符串，失败返回原因。成功后必须调用 release()。"""
        async with self._lock:
            if not exempt:
                left = self.cooldown_left(user_id, cooldown)
                if left:
                    return f"冷却中，请 {left} 秒后再试"
            if self._running >= max(1, max_concurrent):
                from .phrases import pick

                return pick("busy")
            self._running += 1
            self._last_used[user_id] = self._clock()
            return ""

    def release(self, user_id: str, *, success: bool) -> None:
        self._running = max(0, self._running - 1)
        if not success:
            # 失败不算冷却，方便马上重试。
            self._last_used.pop(user_id, None)

    @property
    def running(self) -> int:
        return self._running


def scope_allowed(group_id: str | None, *, allowed_groups: list[str], allow_private: bool) -> bool:
    if group_id is None:
        return allow_private
    return not allowed_groups or str(group_id) in {str(g) for g in allowed_groups}
