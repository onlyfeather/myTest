"""每个会话（群 / 私聊）最近发过哪些作品，搜图时跳过，避免一直刷到同几张。只在内存里。"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable

KEEP_SECONDS = 3 * 24 * 3600  # 三天内发过的不再发
MAX_PER_SCOPE = 2000
MAX_SCOPES = 500


class SeenTracker:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._scopes: OrderedDict[str, OrderedDict[str, float]] = OrderedDict()

    def _scope(self, scope: str) -> OrderedDict[str, float]:
        items = self._scopes.get(scope)
        if items is None:
            items = self._scopes[scope] = OrderedDict()
            while len(self._scopes) > MAX_SCOPES:
                self._scopes.popitem(last=False)
        self._scopes.move_to_end(scope)
        cutoff = self._clock() - KEEP_SECONDS
        while items and next(iter(items.values())) < cutoff:
            items.popitem(last=False)
        return items

    def seen(self, scope: str) -> set[str]:
        return set(self._scope(scope)) if scope else set()

    def mark(self, scope: str, illust_ids: Iterable[str]) -> None:
        if not scope:
            return
        items = self._scope(scope)
        now = self._clock()
        for illust_id in illust_ids:
            items.pop(str(illust_id), None)
            items[str(illust_id)] = now
        while len(items) > MAX_PER_SCOPE:
            items.popitem(last=False)

    def forget(self, scope: str) -> None:
        self._scopes.pop(scope, None)


seen_tracker = SeenTracker()
