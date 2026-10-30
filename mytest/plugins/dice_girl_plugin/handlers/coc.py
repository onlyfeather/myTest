"""CoC 跑团指令：.ra / .rc 技能检定（可带奖励骰、惩罚骰）、.sc 理智检定、.st 角色卡。"""

import re

from nonebot import on_regex
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.params import RegexGroup

from ..dice.coc import apply_st, check, parse_sanity, parse_skill_arg, parse_st, sanity_check, skill_name
from ..dice.expression import DiceError
from ..storage import get_skills, save_skills
from .common import get_user_name
from .dice import react, send_result

SANITY = "理智"

# 指令后面可以直接接中文（.ra侦查、.st理智60），但不能接英文字母，免得 .start 之类的被当成 .st
check_matcher = on_regex(r"^[.。]r[ac]([bp]\d?)?(?![a-z])\s*([\s\S]*)$", flags=re.IGNORECASE, priority=5, block=True)
sanity_matcher = on_regex(r"^[.。]sc(?![a-z])\s*([\s\S]*)$", flags=re.IGNORECASE, priority=5, block=True)
st_matcher = on_regex(r"^[.。]st(?![a-z])\s*([\s\S]*)$", flags=re.IGNORECASE, priority=5, block=True)


def scope_of(event: Event) -> str:
    return f"group:{event.group_id}" if isinstance(event, GroupMessageEvent) else "private"


def parse_bonus(flag: str | None) -> int:
    """「b」→ 1，「p2」→ -2，没有 → 0。"""
    if not flag:
        return 0
    count = int(flag[1:] or 1)
    return count if flag[0].lower() == "b" else -count


@check_matcher.handle()
async def handle_check(event: Event, groups: tuple = RegexGroup()):
    bonus = parse_bonus(groups[0])
    name, value = parse_skill_arg(groups[1] or "")
    if value is None:
        if not name:
            await check_matcher.finish("用法：.ra 技能 [技能值]，例：.ra 侦查 60；先 .st 侦查60 记下来就只用写 .ra 侦查")
        stored = get_skills(event.get_user_id(), scope_of(event)).get(name)
        if stored is None:
            await check_matcher.finish(f"角色卡里没有「{name}」，写上技能值（.ra {name} 60）或者先 .st {name}60")
        value = stored
    try:
        result = check(value, bonus)
    except DiceError as e:
        await check_matcher.finish(str(e))
    event_name = name or "检定"
    reply = await react(event, {
        "max": 100,
        "target": value,
        "event": event_name,
        "final_roll": result.roll,
        "final_status": result.level,
    })
    await send_result(check_matcher, (f"{get_user_name(event)} 的{event_name}：{result.header()}\n\n{reply or '...'}",
                                      reply))


@sanity_matcher.handle()
async def handle_sanity(event: Event, groups: tuple = RegexGroup()):
    user_id, scope = event.get_user_id(), scope_of(event)
    try:
        on_success, on_failure, given = parse_sanity(groups[0] or "")
    except DiceError as e:
        await sanity_matcher.finish(str(e))
    skills = get_skills(user_id, scope)
    san = given if given is not None else skills.get(SANITY)
    if san is None:
        await sanity_matcher.finish("不知道你现在的理智，写在后面（.sc 1/1d6 60）或者先 .st 理智60")
    try:
        result = sanity_check(san, on_success, on_failure)
    except DiceError as e:
        await sanity_matcher.finish(str(e))
    if SANITY in skills:
        save_skills(user_id, scope, {**skills, SANITY: result.after})

    lines = [
        f"{get_user_name(event)} 的理智检定：{result.check.header()}",
        f"理智损失：{result.loss_detail}，{result.before} → {result.after}",
        *(f"⚠ {note}" for note in result.notes()),
    ]
    reply = await react(event, {
        "max": 100,
        "target": san,
        "event": f"理智检定（损失 {result.loss} 点）",
        "final_roll": result.check.roll,
        "final_status": result.check.level,
    })
    await send_result(sanity_matcher, ("\n".join(lines) + f"\n\n{reply or '...'}", reply))


CLEAR_WORDS = {"清空", "clr", "clear", "重置"}
SHOW_WORDS = {"", "show", "显示", "查看"}


def format_sheet(name: str, skills: dict[str, int]) -> str:
    if not skills:
        return f"{name} 的角色卡是空的。用 .st 侦查60 图书馆70 理智50 记录"
    items = "  ".join(f"{k}{v}" for k, v in skills.items())
    return f"📋 {name} 的角色卡（{len(skills)} 项）\n{items}"


@st_matcher.handle()
async def handle_st(event: Event, groups: tuple = RegexGroup()):
    user_id, scope = event.get_user_id(), scope_of(event)
    text = (groups[0] or "").strip()
    skills = get_skills(user_id, scope)
    name = get_user_name(event)

    if text.lower() in SHOW_WORDS:
        await st_matcher.finish(format_sheet(name, skills))
    if text.lower() in CLEAR_WORDS:
        save_skills(user_id, scope, {})
        await st_matcher.finish("角色卡已清空")
    if text.startswith(("删除", "del ")):
        targets = [skill_name(n) for n in re.split(r"[\s,，、]+", text.removeprefix("删除").removeprefix("del ")) if n]
        removed = [n for n in targets if n in skills]
        if not removed:
            await st_matcher.finish("角色卡里没有这些项")
        save_skills(user_id, scope, {k: v for k, v in skills.items() if k not in removed})
        await st_matcher.finish("已删除：" + "、".join(removed))

    try:
        items = parse_st(text)
        updated = apply_st(skills, items)
    except DiceError as e:
        await st_matcher.finish(str(e))
    save_skills(user_id, scope, updated)
    changes = []
    for item_name, op, _ in items:
        old = skills.get(item_name)
        new = updated[item_name]
        changes.append(f"{item_name} {old}→{new}" if op and old is not None else f"{item_name}{new}")
    await st_matcher.finish(f"已记录（{len(updated)} 项）：" + "  ".join(dict.fromkeys(changes)))
