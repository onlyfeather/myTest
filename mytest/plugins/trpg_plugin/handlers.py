"""收发：只在配置的跑团群里响应。指令、角色行动、撤回、后台计时。"""

from __future__ import annotations

import asyncio
import contextlib
import time

from nonebot import get_bots, get_driver, logger, on_command, on_message, on_notice
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, GroupRecallNoticeEvent, Message
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import Rule

from .campaign import Actor, Campaigns, ModuleLibrary
from .commands import COMMANDS, Caller, run
from .config import DATA_DIR, timing, trpg_groups
from .play import settle_placeholder
from .rounds import OOC_REPLY_WORDS, Rounds
from .store import Store

TICK_SECONDS = 30

store = Store(DATA_DIR / "trpg.db")
campaigns = Campaigns(store, ModuleLibrary(store))
rounds = Rounds(campaigns)


async def _in_trpg_group(event: GroupMessageEvent) -> bool:
    return str(event.group_id) in trpg_groups()


async def is_manager(bot: Bot, event: GroupMessageEvent) -> bool:
    return bool(await SUPERUSER(bot, event) or await GROUP_ADMIN(bot, event) or await GROUP_OWNER(bot, event))


def _load_sheet(user_id: str, group_id: str):
    def load() -> dict[str, int]:
        from ..dice_girl_plugin.storage import get_skills

        return get_skills(user_id, f"group:{group_id}")

    return load


# ---------------------------------------------------------------- 指令


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


# ---------------------------------------------------------------- 角色行动

# 优先级排在所有指令后面，也不拦截：.ra、/帮助 这些照常由别的插件处理
action_matcher = on_message(rule=Rule(_in_trpg_group), priority=50, block=False)


@action_matcher.handle()
async def handle_action(event: GroupMessageEvent):
    group_id, user_id = str(event.group_id), event.get_user_id()
    text = event.get_plaintext().strip()
    now = int(time.time())
    if event.reply is not None and text in OOC_REPLY_WORDS:
        note = rounds.mark_ooc(group_id, user_id, str(event.reply.message_id), now)
        if note:
            await action_matcher.finish(note)
        return
    result = rounds.receive(group_id, user_id, str(event.message_id), text, now)
    if result.note:
        await action_matcher.send(result.note)
    if result.questions:
        await action_matcher.send("（场外提问记下了。KP 回答场外问题的功能还在开发中。）")


async def _is_trpg_recall(event: GroupRecallNoticeEvent) -> bool:
    return str(event.group_id) in trpg_groups()


recall_matcher = on_notice(rule=Rule(_is_trpg_recall), priority=50, block=False)


@recall_matcher.handle()
async def handle_recall(event: GroupRecallNoticeEvent):
    rounds.recalled(str(event.group_id), str(event.message_id), int(time.time()))


# ---------------------------------------------------------------- 后台计时


async def _send(group_id: str, text: str) -> None:
    bots = list(get_bots().values())
    if not bots:
        logger.warning("[跑团] QQ 机器人不在线，消息没发出去：{}", text[:40])
        return
    await bots[0].send_group_msg(group_id=int(group_id), message=text)


async def tick_once(now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    result = rounds.tick(now, timing())
    for group_id, text in result.notices:
        await _send(group_id, text)
    for campaign_id, round_no in result.collected:
        text = settle_placeholder(campaigns, rounds, campaign_id, round_no, now)
        campaign = campaigns.get(campaign_id)
        if text and campaign is not None:
            await _send(campaign.group_id, text)


async def _loop() -> None:
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await tick_once()
        except Exception as e:  # 后台任务不能因为一次出错就停掉
            logger.opt(exception=e).warning("[跑团] 计时出错")


_task: asyncio.Task | None = None
driver = get_driver()


@driver.on_startup
async def _start() -> None:
    global _task
    _task = asyncio.get_running_loop().create_task(_loop())


@driver.on_shutdown
async def _stop() -> None:
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
    store.close()
