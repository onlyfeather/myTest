"""收发：只在配置的跑团群里响应。指令、角色行动、孤注一掷 / 花幸运的回答、场外提问、撤回、后台计时。

所有要发到群里的话都先写进发送队列（outbox），由后台发出；发送失败会重试，重启后接着发。
"""

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

from .._shared.settings import ItemManager, ManagerAction, settings_store
from .._shared.status import StatusRow, status_registry
from .ai import ModelClient
from .campaign import RUNNING, Actor, Campaigns, ModuleLibrary
from .commands import COMMANDS, Caller, run
from .config import DATA_DIR, endpoints, output_mode, review_enabled, timing, trpg_groups
from .manage import CampaignManager, ModuleManager
from .outbox import Outbox
from .play import Engine
from .rounds import OOC_REPLY_WORDS, Rounds
from .store import Store

TICK_SECONDS = 30
FLUSH_SECONDS = 2

store = Store(DATA_DIR / "trpg.db")
campaigns = Campaigns(store, ModuleLibrary(store))
rounds = Rounds(campaigns)
outbox = Outbox(store)
_last_calls: dict[int, str] = {}
_wake = asyncio.Event()


def _remember(campaign_id: int, round_no: int, stage: str, attempt) -> None:
    state = "成功" if attempt.ok else f"失败：{attempt.error[:60]}"
    _last_calls[campaign_id] = f"第 {round_no} 轮 {stage} · {attempt.label} {attempt.model} · {attempt.duration_ms} ms · {state}"


engine = Engine(campaigns, rounds, ModelClient(mode=output_mode), endpoints, log_attempt=_remember,
                review_enabled=review_enabled)


def _say(group_id: str, *texts: str) -> None:
    outbox.put(group_id, [t for t in texts if t], int(time.time()))
    _wake.set()


async def _in_trpg_group(event: GroupMessageEvent) -> bool:
    return str(event.group_id) in trpg_groups()


async def is_manager(bot: Bot, event: GroupMessageEvent) -> bool:
    return bool(await SUPERUSER(bot, event) or await GROUP_ADMIN(bot, event) or await GROUP_OWNER(bot, event))


def _load_sheet(user_id: str, group_id: str):
    def load() -> dict[str, int]:
        from ..dice_girl_plugin.storage import get_skills

        return get_skills(user_id, f"group:{group_id}")

    return load


async def _send(group_id: str, text: str) -> None:
    bots = list(get_bots().values())
    if not bots:
        raise RuntimeError("QQ 机器人不在线")
    await bots[0].send_group_msg(group_id=int(group_id), message=text)


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
        _say(group_id, run(campaigns, command, args.extract_plain_text(), caller))
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
        _say(group_id, rounds.mark_ooc(group_id, user_id, str(event.reply.message_id), now))
        return
    if await engine.answer(group_id, user_id, text) is not None:
        _wake.set()
        return
    result = rounds.receive(group_id, user_id, str(event.message_id), text, now)
    _say(group_id, result.note)
    for question in result.questions:
        await engine.ask(group_id, user_id, question)
        _wake.set()


async def _is_trpg_recall(event: GroupRecallNoticeEvent) -> bool:
    return str(event.group_id) in trpg_groups()


recall_matcher = on_notice(rule=Rule(_is_trpg_recall), priority=50, block=False)


@recall_matcher.handle()
async def handle_recall(event: GroupRecallNoticeEvent):
    rounds.recalled(str(event.group_id), str(event.message_id), int(time.time()))


# ---------------------------------------------------------------- 后台：计时、结算、发送、重启恢复


async def tick_once(now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    result = rounds.tick(now, timing())
    for group_id, text in result.notices:
        _say(group_id, text)
    for group_id, text in engine.expire_choices():
        _say(group_id, text)
    # 收齐的、结算到一半的（重启前没做完的）、选择回答完了的：都在这里接着做。要发的话 engine 自己写进队列
    for campaign in campaigns.unfinished():
        if campaign.phase != RUNNING or campaign.group_id not in trpg_groups():
            continue
        try:
            await engine.settle(campaign.id)
        except Exception as e:  # 一个团出错不影响别的团
            logger.opt(exception=e).warning("[跑团] 群 {} 结算出错", campaign.group_id)


async def _tick_loop() -> None:
    store.reset_working_choices()
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await tick_once()
        except Exception as e:  # 后台任务不能因为一次出错就停掉
            logger.opt(exception=e).warning("[跑团] 计时出错")
        _wake.set()


async def _send_loop() -> None:
    while True:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), FLUSH_SECONDS)
        _wake.clear()
        try:
            await outbox.flush(_send, int(time.time()))
        except Exception as e:
            logger.opt(exception=e).warning("[跑团] 发送出错")


# ---------------------------------------------------------------- 运行状态、WebUI


def trpg_status() -> list[StatusRow]:
    rows = []
    groups = trpg_groups()
    if not groups:
        return [StatusRow("跑团群", "没有填，在「跑团」里配置")]
    rows.append(StatusRow("跑团群", "、".join(sorted(groups))))
    if not endpoints("arbiter"):
        rows.append(StatusRow("模型", "没有可用的模型：在「大模型」里填接口地址和密钥", "error"))
    stats = store.outbox_stats()
    if stats.get("failed"):
        rows.append(StatusRow("发送失败", f"{stats['failed']} 条消息重试 5 次都没发出去", "error"))
    if stats.get("pending"):
        rows.append(StatusRow("待发消息", f"{stats['pending']} 条", "warn"))
    for c in campaigns.unfinished():
        rows.append(StatusRow(f"群 {c.group_id}", f"「{c.module_title}」第 {c.round} 轮"))
        if c.id in _last_calls:
            rows.append(StatusRow("最近一次模型调用", _last_calls[c.id], "error" if "失败" in _last_calls[c.id] else "ok"))
    return rows


status_registry.register("trpg", "跑团", trpg_status, order=60)

_modules = ModuleManager(campaigns)
_games = CampaignManager(engine)

settings_store.set_manager("trpg", ItemManager(
    title="模组和进行中的团",
    help="上面是模组，下面是进行中的团。模组格式见 docs/trpg-module-format.md；"
         "「KP 视角」会显示真相和所有秘密，只在这里看，不要发到群里。",
    items=lambda: _both_items(),
    actions=(
        ManagerAction("upload", "上传模组", scope="global", input="text", primary=True,
                      placeholder="把模组 JSON 整段粘贴进来（可以带 // 注释）"),
        ManagerAction("check", "校验结果"),
        ManagerAction("delete", "删除模组", confirm="删除模组「{title}」？"),
        ManagerAction("state", "KP 视角"),
        ManagerAction("log", "最近日志"),
        ManagerAction("retry", "马上重试"),
        ManagerAction("reveal", "揭示线索", input="text", placeholder="线索编号，例如 c2"),
        ManagerAction("hide", "收回线索", input="text", placeholder="线索编号，例如 c2"),
        ManagerAction("edit", "改数值", input="lines", placeholder="每行一个：调查员名 理智50 幸运40"),
        ManagerAction("end", "结束这个团", confirm="结束{title}？群里会收到一句「结束了」"),
    ),
    run=lambda action, item_id, value: _both_run(action, item_id, value),
))

_MODULE_ACTIONS = {"upload", "check", "delete"}


async def _both_items() -> dict:
    mods = await _modules.items()
    games = await _games.items()
    for item in mods["items"]:
        item["id"] = f"m:{item['id']}"
        item["badges"] = ["模组", *item.get("badges", [])]
    for item in games["items"]:
        item["id"] = f"c:{item['id']}"
        item["badges"] = ["团", *item.get("badges", [])]
    notes = [n for n in (mods["notice"], games["notice"]) if n]
    return {"items": mods["items"] + games["items"], "notice": "；".join(notes)}


async def _both_run(action: str, item_id: str, value) -> str:
    kind, _, raw = item_id.partition(":")
    if action in _MODULE_ACTIONS:
        if action != "upload" and kind != "m":
            raise ValueError("这个按钮只对模组有用")
        result = await _modules.run(action, raw, value)
    else:
        if kind != "c":
            raise ValueError("这个按钮只对进行中的团有用")
        result = await _games.run(action, raw, value)
    _wake.set()
    return result


# ---------------------------------------------------------------- 启动和停止

_tasks: list[asyncio.Task] = []
driver = get_driver()


@driver.on_startup
async def _start() -> None:
    loop = asyncio.get_running_loop()
    _tasks.extend([loop.create_task(_tick_loop()), loop.create_task(_send_loop())])


@driver.on_shutdown
async def _stop() -> None:
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    store.close()
