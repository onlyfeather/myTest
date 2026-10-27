import re

from nonebot import on_command, on_regex
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.params import CommandArg, RegexStr
from nonebot.permission import SUPERUSER

from ..llm import is_ai_enabled, set_ai_enabled
from ..persona import persona_manager
from ..storage import set_user_favorability
from .common import extract_plain_text


async def private_superuser(bot: Bot, event: Event) -> bool:
    return isinstance(event, PrivateMessageEvent) and await SUPERUSER(bot, event)


ai_switch_matcher = on_regex(
    r"^/dice\s+ai(?:\s+.*)?$",
    flags=re.IGNORECASE,
    permission=private_superuser,
    priority=1,
)

admin_matcher = on_command(
    "dice 设置好感",
    permission=private_superuser,
    priority=1,
)



@ai_switch_matcher.handle()
async def handle_ai_switch(event: Event, regex_str: str = RegexStr()):
    text = (regex_str or extract_plain_text(event)).strip()
    action = re.sub(r"^/dice\s+ai\b", "", text, flags=re.IGNORECASE).strip().lower()

    if action in {"on", "enable", "enabled", "开", "开启", "启用", "true", "1"}:
        set_ai_enabled(True)
        await ai_switch_matcher.finish("✅ 骰娘 AI 已开启。")

    if action in {"off", "disable", "disabled", "关", "关闭", "停用", "false", "0"}:
        set_ai_enabled(False)
        await ai_switch_matcher.finish("✅ 骰娘 AI 已关闭。")

    if action in {"", "status", "状态"}:
        status = "开启" if is_ai_enabled() else "关闭"
        await ai_switch_matcher.finish(f"当前骰娘 AI 状态：{status}")

    await ai_switch_matcher.finish("格式错误: /dice AI 开启|关闭|状态")


@admin_matcher.handle()
async def handle_admin(event: Event, args: Message = CommandArg()):
    msg = args.extract_plain_text().split()
    if len(msg) < 2:
        await admin_matcher.finish("格式错误: /dice 设置好感 [user_id] [数值] [角色ID(可选)]")
        return

    target_id = msg[0]
    try:
        val = int(msg[1])
    except ValueError:
        await admin_matcher.finish("数值必须是整数")
        return

    if len(msg) >= 3:
        target_role = msg[2]
    else:
        target_role = persona_manager.get_current_role_id(event)

    set_user_favorability(target_id, val, role_id=target_role)
    await admin_matcher.finish(
        f"✅ 已将用户 {target_id} 对角色【{target_role}】的好感度强制设定为 {val}。"
    )
