import re

from nonebot import logger, on_command, on_regex
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg, RegexStr

from ..config import dice_settings
from ..dice import dice_logic
from ..llm import get_dice_reaction
from ..persona import persona_manager
from ..storage import get_user_favorability
from .common import get_user_name

dice_matcher = on_command(
    "dice 投骰",
    priority=5,
)

rd_shortcut_matcher = on_regex(r"^[.。]rd.*", flags=re.IGNORECASE, priority=5)


async def _handle_dice_text(bot: Bot, event: Event, raw_text: str):
    user_id = event.get_user_id()
    user_name = get_user_name(event)
    role_key = persona_manager.get_current_role_id(event)

    parsed = dice_logic.parse_command(raw_text)
    if not parsed:
        return

    max_val, target_val, event_name = parsed
    current_fav = get_user_favorability(user_id, role_id=role_key)

    result = dice_logic.execute_roll(max_val, target_val, event_name)

    ai_result = await get_dice_reaction(current_fav, result, user_name, event=event)
    logger.debug("[Dice] AI response received")

    reply_text = ai_result.get("reply", "") or "..."

    if result["target"] is None:
        header = f"1D{max_val}={result['final_roll']}"
    else:
        header = f"1D{max_val}={result['final_roll']}/{target_val} 【{result['final_status']}】"

    return f"{header}\n\n{reply_text}", reply_text if ai_result.get("reply") else ""


async def _send_result(matcher: type[Matcher], result) -> None:
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
    await _send_result(dice_matcher, await _handle_dice_text(bot, event, args.extract_plain_text()))


@rd_shortcut_matcher.handle()
async def handle_rd_shortcut(bot: Bot, event: Event, regex_str: str = RegexStr()):
    await _send_result(rd_shortcut_matcher, await _handle_dice_text(bot, event, regex_str))
