"""发送队列：要发到群里的话先写进数据库，再一条条发。不依赖 NoneBot。

- 发送失败按 10 秒、1 分钟、5 分钟、15 分钟重试，5 次都失败标成 failed，运行状态页能看到。
- 同一个群按顺序发：前面一条还没发出去，后面的等着。
- 重启后接着发没发完的；发过的不重发。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .store import Store

RETRY_DELAYS = (10, 60, 300, 900)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
MAX_MESSAGE = 1500  # QQ 一条太长会被折叠，分段
KEEP_SENT_DAYS = 7

Sender = Callable[[str, str], Awaitable[None]]  # (群号, 文字)


def split(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """按段落切，单独一段太长再硬切。"""
    parts: list[str] = []
    current = ""
    for para in text.split("\n"):
        while len(para) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(para[:limit])
            para = para[limit:]
        if current and len(current) + len(para) + 1 > limit:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n{para}" if current else para
    if current:
        parts.append(current)
    return [p for p in parts if p.strip()]


class Outbox:
    def __init__(self, store: Store) -> None:
        self.store = store

    def put(self, group_id: str, messages: list[str], now: int) -> None:
        texts: list[str] = []
        for m in messages:
            texts += split(m)
        self.store.enqueue(group_id, texts, now)

    async def flush(self, send: Sender, now: int) -> int:
        """发该发的消息，返回发出去几条。一个群发送失败后，这个群这一次不再往下发。"""
        sent = 0
        failed_groups: set[str] = set()
        for row in self.store.due_messages(now):
            if row["group_id"] in failed_groups:
                continue
            try:
                await send(row["group_id"], row["text"])
            except Exception as e:  # 发送失败（机器人掉线、被风控）都重试
                attempts = row["attempts"] + 1
                give_up = attempts >= MAX_ATTEMPTS
                delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
                self.store.mark_send_failed(row["id"], f"{type(e).__name__}: {e}", now + delay, give_up)
                failed_groups.add(row["group_id"])
                continue
            self.store.mark_sent(row["id"])
            sent += 1
        self.store.prune_outbox(now - KEEP_SENT_DAYS * 86400)
        return sent
