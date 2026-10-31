"""收发：只在配置的跑团群里响应。指令、角色行动、孤注一掷 / 花幸运的回答、场外提问、撤回、后台计时。"""

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

from .._shared.status import StatusRow, status_registry
from .ai import ModelClient
from .campaign import RUNNING, Actor, Campaigns, ModuleLibrary
from .commands import COMMANDS, Caller, run
from .config import DATA_DIR, endpoints, output_mode, timing, trpg_groups
from .play import Engine
from .rounds import OOC_REPLY_WORDS, Rounds
from .store import Store

TICK_SECONDS = 30
MAX_MESSAGE = 1500  # QQ 一条消息太长会被折叠，分段发

store = Store(DATA_DIR / "trpg.db")
campaigns = Campaigns(store, ModuleLibrary(store))
rounds = Rounds(campaigns)
_last_calls: dict[int, str] = {}


def _remember(campaign_id: int, round_no: int, stage: str, attempt) -> None:
    state = "成功" if attempt.ok else f"失败：{attempt.error[:60]}"
    _last_calls[campaign_id] = f"第 {round_no} 轮 {stage} · {attempt.label} {attempt.model} · {attempt.duration_ms} ms · {state}"


engine = Engine(campaigns, rounds, ModelClient(mode=output_mode), endpoints, log_attempt=_remember)


async def _in_trpg_group(event: GroupMessageEvent) -> bool:
    return str(event.group_id) in trpg_groups()


async def is_manager(bot: Bot, event: GroupMessageEvent) -> bool:
    return bool(await SUPERUSER(bot, event) or await GROUP_ADMIN(bot, event) or await GROUP_OWNER(bot, event))


def _load_sheet(user_id: str, group_id: str):
    def load() -> dict[str, int]:
        from ..dice_girl_plugin.storage import get_skills

        return get_skills(user_id, f"group:{group_id}")

    return load


def _split(text: str) -> list[str]:
    parts, current = [], ""
    for para in text.split("\n"):
        if current and len(current) + len(para) + 1 > MAX_MESSAGE:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n{para}" if current else para
    if current:
        parts.append(current)
    return [p for p in parts if p.strip()]


async def _send(group_id: str, text: str) -> None:
    bots = list(get_bots().values())
    if not bots:
        logger.warning("[跑团] QQ 机器人不在线，消息没发出去：{}", text[:40])
        return
    for part in _split(text):
        await bots[0].send_group_msg(group_id=int(group_id), message=part)


async def _send_all(group_id: str, messages: list[str]) -> None:
    for text in messages:
        if text and text.strip():
            await _send(group_id, text)


# ---------------------------------------------------------------- 指令


def _make(command: str):
    matcher = on_command(command, rule=Rule(_in_trpg_group), priority=5, block=True)

    @matcher.handle()
    async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
        user_id, group_id = event.get_user_id(), str(event.group_id)
        sender = event.sender
        active = campaigns.active(group_id)
        caller = Caller(
            group_id=group_id,
            actor=Actor(user_id, await is_manager(bot, event)),
            name=(sender.card or sender.nickname or "") if sender else "",
            load_sheet=_load_sheet(user_id, group_id),
            waiting_choices=(lambda: engine.waiting_choices(active.id)) if active else None,
            retry=engine.retry_now,
        )
        reply = run(campaigns, command, args.extract_plain_text(), caller)
        if reply:
            await _send(group_id, reply)
        await matcher.finish()

    return matcher


matchers = {name: _make(name) for name in COMMANDS}


# ---------------------------------------------------------------- 角色行动、选择、场外提问

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
            await _send(group_id, note)
        return
    answered = await engine.answer(group_id, user_id, text)
    if answered is not None:
        await _send_all(group_id, answered)
        return
    result = rounds.receive(group_id, user_id, str(event.message_id), text, now)
    if result.note:
        await _send(group_id, result.note)
    for question in result.questions:
        reply = await engine.ask(group_id, user_id, question)
        if reply:
            await _send(group_id, reply)


async def _is_trpg_recall(event: GroupRecallNoticeEvent) -> bool:
    return str(event.group_id) in trpg_groups()


recall_matcher = on_notice(rule=Rule(_is_trpg_recall), priority=50, block=False)


@recall_matcher.handle()
async def handle_recall(event: GroupRecallNoticeEvent):
    rounds.recalled(str(event.group_id), str(event.message_id), int(time.time()))


# ---------------------------------------------------------------- 后台：计时、结算、重启恢复


async def tick_once(now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    result = rounds.tick(now, timing())
    for group_id, text in result.notices:
        await _send(group_id, text)
    for group_id, text in engine.expire_choices():
        await _send(group_id, text)
    # 收齐的、结算到一半的（重启前没做完的）、选择回答完了的：都在这里接着做
    for campaign in campaigns.unfinished():
        if campaign.phase != RUNNING or campaign.group_id not in trpg_groups():
            continue
        try:
            messages = await engine.settle(campaign.id)
        except Exception as e:  # 一个团出错不影响别的团
            logger.opt(exception=e).warning("[跑团] 群 {} 结算出错", campaign.group_id)
            continue
        await _send_all(campaign.group_id, messages)


async def _loop() -> None:
    store.reset_working_choices()
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await tick_once()
        except Exception as e:  # 后台任务不能因为一次出错就停掉
            logger.opt(exception=e).warning("[跑团] 计时出错")


def trpg_status() -> list[StatusRow]:
    rows = []
    groups = trpg_groups()
    if not groups:
        return [StatusRow("跑团群", "没有填，在「跑团」里配置")]
    rows.append(StatusRow("跑团群", "、".join(sorted(groups))))
    if not endpoints("arbiter"):
        rows.append(StatusRow("模型", "没有可用的模型：在「大模型」里填接口地址和密钥", "error"))
    for c in campaigns.unfinished():
        rows.append(StatusRow(f"群 {c.group_id}", f"「{c.module_title}」第 {c.round} 轮"))
        if c.id in _last_calls:
            rows.append(StatusRow("最近一次模型调用", _last_calls[c.id], "error" if "失败" in _last_calls[c.id] else "ok"))
    return rows


status_registry.register("trpg", "跑团", trpg_status, order=60)

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
