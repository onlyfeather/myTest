"""收发：只在配置的跑团群里响应指令。"""

from __future__ import annotations

from nonebot import on_command
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import Rule

from .campaign import Actor, Campaigns, ModuleLibrary
from .commands import COMMANDS, Caller, run
from .config import DATA_DIR, trpg_groups
from .store import Store

store = Store(DATA_DIR / "trpg.db")
campaigns = Campaigns(store, ModuleLibrary(store))


async def _in_trpg_group(event: GroupMessageEvent) -> bool:
    return str(event.group_id) in trpg_groups()


async def is_manager(bot: Bot, event: GroupMessageEvent) -> bool:
    return bool(await SUPERUSER(bot, event) or await GROUP_ADMIN(bot, event) or await GROUP_OWNER(bot, event))


def _load_sheet(user_id: str, group_id: str):
    def load() -> dict[str, int]:
        from ..dice_girl_plugin.storage import get_skills

        return get_skills(user_id, f"group:{group_id}")

    return load


def _make(command: str):
    matcher = on_command(command, rule=Rule(_in_trpg_group), priority=5, block=True)

    @matcher.handle()
    async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
        user_id, group_id = event.get_user_id(), str(event.group_id)
        sender = event.sender
        caller = Caller(
            group_id=group_id,
            actor=Actor(user_id, await is_manager(bot, event)),
            name=(sender.card or sender.nickname or "") if sender else "",
            load_sheet=_load_sheet(user_id, group_id),
        )
        reply = run(campaigns, command, args.extract_plain_text(), caller)
        if reply:
            await matcher.finish(reply)

    return matcher


matchers = {name: _make(name) for name in COMMANDS}
