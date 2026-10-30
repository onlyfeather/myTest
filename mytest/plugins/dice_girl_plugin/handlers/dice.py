import re

from nonebot import logger, on_command, on_regex
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg, RegexStr

from ..config import dice_settings
from ..dice import dice_logic
from ..dice.expression import DiceError, roll
from ..llm import get_dice_reaction
from ..persona import persona_manager
from ..storage import get_user_favorability
from .common import get_user_name

dice_matcher = on_command(
    "dice 投骰",
    priority=5,
)

rd_shortcut_matcher = on_regex(r"^[.。]rd.*", flags=re.IGNORECASE, priority=5)

# 第一个词里带 d 的按表达式掷：3d6+2、d20、2d6+1d4
_EXPRESSION_HEAD = re.compile(r"^[0-9+\-]*d[0-9dD+\-]*(?:\s|$)", re.IGNORECASE)


async def react(event: Event, data: dict) -> str:
    """让点评角色说一句。返回点评文字；AI 没有给出点评时返回空字符串。"""
    role_key = persona_manager.get_current_role_id(event)
    current_fav = get_user_favorability(event.get_user_id(), role_id=role_key)
    ai_result = await get_dice_reaction(current_fav, data, get_user_name(event), event=event)
    logger.debug("[Dice] AI response received")
    return ai_result.get("reply", "") or ""


async def _handle_expression(event: Event, body: str):
    # 第一个词已经确定是表达式（带 d），写错了要报错，不能当成事件名掷 1D100
    expression, _, event_name = body.partition(" ")
    event_name = event_name.strip()
    try:
        result = roll(expression)
    except DiceError as e:
        return str(e), ""
    data = {
        "max": result.maximum,
        "min": result.minimum,
        "target": None,
        "event": event_name or "随机数",
        "final_roll": result.total,
        "final_status": "无判定",
        "expression": result.expression,
    }
    reply = await react(event, data)
    prefix = f"{event_name}：" if event_name else ""
    return f"{prefix}{result.detail}\n\n{reply or '...'}", reply


async def _handle_dice_text(bot: Bot, event: Event, raw_text: str):
    body = re.sub(r"^[.。]rd", "", raw_text, flags=re.IGNORECASE).strip()
    if _EXPRESSION_HEAD.match(body):
        return await _handle_expression(event, body)

    parsed = dice_logic.parse_command(raw_text)
    if not parsed:
        return None

    max_val, target_val, event_name = parsed
    result = dice_logic.execute_roll(max_val, target_val, event_name)
    reply_text = await react(event, result)

    if result["target"] is None:
        header = f"1D{max_val}={result['final_roll']}"
    else:
        header = f"1D{max_val}={result['final_roll']}/{target_val} 【{result['final_status']}】"

    return f"{header}\n\n{reply_text or '...'}", reply_text


async def send_result(matcher: type[Matcher], result) -> None:
    if not result:
        return
    full_text, spoken = result
    await matcher.send(full_text)
    if not (spoken and dice_settings.voice_reply):
        return
    # 用到时才导入，这样没加载语音插件时骰娘照常工作。
    try:
        from ...tts_plugin.service import is_ready, synthesize
    except ImportError:
        return
    if is_ready():
        try:
            await matcher.send(MessageSegment.record(await synthesize(spoken)))
        except Exception as e:
            logger.warning("[Dice] 点评语音合成失败: {}", e)


@dice_matcher.handle()
async def handle_dice(bot: Bot, event: Event, args: Message = CommandArg()):
    await send_result(dice_matcher, await _handle_dice_text(bot, event, args.extract_plain_text()))


@rd_shortcut_matcher.handle()
async def handle_rd_shortcut(bot: Bot, event: Event, regex_str: str = RegexStr()):
    await send_result(rd_shortcut_matcher, await _handle_dice_text(bot, event, regex_str))
