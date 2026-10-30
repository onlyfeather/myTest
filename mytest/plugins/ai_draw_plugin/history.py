"""每个人最近一次画的图：/再画 换个种子重画，/改图 在这张图上修改。只存在内存里，重启就没了。"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

MAX_ENTRIES = 30  # 每条都带着图片，几十条就有几十 MB，不多存
TTL = 60 * 60


@dataclass
class LastDraw:
    provider: str  # novelai / gpt
    size: str
    prompt: str  # 实际用的描述（NovelAI 是翻译后的 tag），再画时不用重新翻译
    image: bytes
    artists: str | None = None
    references: list[bytes] = field(default_factory=list)
    with_style: bool = False
    created: float = 0.0


class DrawHistory:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._items: OrderedDict[str, LastDraw] = OrderedDict()

    @staticmethod
    def key(user_id: str, group_id: str | None) -> str:
        return f"{group_id or 'private'}:{user_id}"

    def remember(self, key: str, item: LastDraw) -> None:
        item.created = self._clock()
        self._items[key] = item
        self._items.move_to_end(key)
        while len(self._items) > MAX_ENTRIES:
            self._items.popitem(last=False)

    def get(self, key: str) -> LastDraw | None:
        item = self._items.get(key)
        if item is None:
            return None
        if self._clock() - item.created > TTL:
            self._items.pop(key, None)
            return None
        return item


history = DrawHistory()
