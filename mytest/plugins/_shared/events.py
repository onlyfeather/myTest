from __future__ import annotations

from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.permission import SUPERUSER


def group_id_of(event: Event) -> str | None:
    return str(event.group_id) if isinstance(event, GroupMessageEvent) else None


async def is_superuser(bot: Bot, event: Event) -> bool:
    return await SUPERUSER(bot, event)
