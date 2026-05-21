import re

from nonebot import logger, on_command, on_regex
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message
from nonebot.params import CommandArg, RegexStr

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

    result = dice_logic.execute_roll(
        user_id,
        current_fav,
        max_val,
        target_val,
        event_name,
        event=event,
    )

    ai_result = await get_dice_reaction(current_fav, result, user_name, event=event)
    logger.debug("[Dice] AI response received")

    reply_text = ai_result.get("reply", "")
    if not reply_text:
        reply_text = "..."

    sign = "+" if result["mod"] > 0 else ""
    if result["target"] is None:
        header = f"1D{max_val}={result['final_roll']}"
    elif result["is_revealed"]:
        header = (
            f"1D{max_val}={result['raw_roll']}{sign}{result['mod']}="
            f"{result['final_roll']}/{target_val} 【{result['final_status']}】"
        )
    else:
        header = f"1D{max_val}={result['final_roll']}/{target_val} 【{result['final_status']}】"

    return f"{header}\n\n{reply_text}"


@dice_matcher.handle()
async def handle_dice(bot: Bot, event: Event, args: Message = CommandArg()):
    full_text = await _handle_dice_text(bot, event, args.extract_plain_text())
    await dice_matcher.finish(full_text)


@rd_shortcut_matcher.handle()
async def handle_rd_shortcut(bot: Bot, event: Event, regex_str: str = RegexStr()):
    full_text = await _handle_dice_text(bot, event, regex_str)
    await rd_shortcut_matcher.finish(full_text)
