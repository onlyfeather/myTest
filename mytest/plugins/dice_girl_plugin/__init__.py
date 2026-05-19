from nonebot import on_message, on_command, on_regex, logger
from nonebot.adapters import Bot, Event
# 【重要】从 onebot.v11 导入 Message, 避免 NotImplementedError
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, Message
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg, RegexStr
from nonebot.permission import SUPERUSER
import re

from .logic import dice_logic
from .data_source import (
    get_user_favorability,
    update_user_favorability,
    get_user_stats,
    set_user_favorability,
    set_group_role,
    set_private_role
)
from .ai import analyze_chat, get_dice_reaction, is_ai_enabled, set_ai_enabled
from .persona_manager import persona_manager
from .memory import memory_manager


# ========================================
# 辅助函数
# ========================================

def get_user_name(event: Event) -> str:
    sender = getattr(event, "sender", None)
    if sender:
        return getattr(sender, "card", "") or getattr(sender, "nickname", "") or "用户"
    return "用户"


def extract_plain_text(event: Event) -> str:
    if hasattr(event, "get_plaintext"):
        return event.get_plaintext()
    return event.get_plain_text()


async def chat_rule_check(event: Event, bot: Bot) -> bool:
    text = extract_plain_text(event)

    if text.startswith((".", "。", "/")):
        return False

    if isinstance(event, PrivateMessageEvent):
        return True
    if isinstance(event, GroupMessageEvent):
        if event.is_tome():
            return True
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return True

    return False


# ========================================
# 核心指令注册
# ========================================

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

    # 执行逻辑
    result = dice_logic.execute_roll(user_id, current_fav, max_val, target_val, event_name, event=event)

    # 调用 AI
    ai_result = await get_dice_reaction(current_fav, result, user_name, event=event)
    logger.debug("[Dice] AI response received")

    reply_text = ai_result.get("reply", "")
    if not reply_text:
        reply_text = "..."

    # 骰子模式：忽略语音字段

    # 构建头部
    sign = "+" if result['mod'] > 0 else ""
    if result['target'] is None:
        header = f"1D{max_val}={result['final_roll']}"
    else:
        if result['is_revealed']:
            header = f"1D{max_val}={result['raw_roll']}{sign}{result['mod']}={result['final_roll']}/{target_val} 【{result['final_status']}】"
        else:
            header = f"1D{max_val}={result['final_roll']}/{target_val} 【{result['final_status']}】"

    full_text = f"{header}\n\n{reply_text}"

    return full_text


@dice_matcher.handle()
async def handle_dice(bot: Bot, event: Event, args: Message = CommandArg()):
    full_text = await _handle_dice_text(bot, event, args.extract_plain_text())
    await dice_matcher.finish(full_text)


@rd_shortcut_matcher.handle()
async def handle_rd_shortcut(bot: Bot, event: Event, regex_str: str = RegexStr()):
    full_text = await _handle_dice_text(bot, event, regex_str)
    await rd_shortcut_matcher.finish(full_text)


chat_matcher = on_message(rule=chat_rule_check, priority=99, block=False)


@chat_matcher.handle()
async def handle_chat(bot: Bot, event: Event):
    user_id = event.get_user_id()
    user_name = get_user_name(event)
    role_key = persona_manager.get_current_role_id(event)

    if hasattr(event, "get_plaintext"):
        text = event.get_plaintext().strip()
    else:
        text = event.get_plain_text().strip()

    if not text:
        return

    current_fav = get_user_favorability(user_id, role_id=role_key)
    history = memory_manager.get_history(user_id)

    # 调用 AI
    ai_result = await analyze_chat(current_fav, text, user_name, history, event=event)
    logger.debug("[Chat] AI response received")

    reply_text = ai_result.get("reply", "")
    if not reply_text:
        reply_text = "..."

    delta = ai_result.get("delta", 0)
    update_user_favorability(user_id, delta, role_id=role_key)

    memory_manager.add_message(user_id, "user", text)
    memory_manager.add_message(user_id, "assistant", reply_text)

    await chat_matcher.finish(reply_text)


# ========================================
# 实用工具指令
# ========================================

status_matcher = on_command(
    "dice 状态",
    priority=10,
)


@status_matcher.handle()
async def handle_status(bot: Bot, event: Event):
    user_id = event.get_user_id()
    user_name = get_user_name(event)
    role_key = persona_manager.get_current_role_id(event)

    current_persona = persona_manager.get_persona(event)
    role_name = current_persona.get("meta", {}).get("name", "未知")

    data = get_user_stats(user_id, role_id=role_key)
    fav = data["fav"]
    count = data["count"]

    title = "路人"
    if fav >= 90:
        title = "至死不渝的恋人"
    elif fav >= 75:
        title = "亲密挚友"
    elif fav >= 60:
        title = "熟悉的朋友"
    elif fav <= 10:
        title = "一生之敌"
    elif fav <= 20:
        title = "讨厌鬼"

    if fav >= 86:
        comment = f"{role_name} 对你的态度已经明显偏向亲近，聊天时会更主动、更柔软，也更容易露出私心。"
    elif fav >= 60:
        comment = f"{role_name} 已经开始在意你的回应，聊天时会有更多别扭的关心和试探。"
    elif fav <= 20:
        comment = f"{role_name} 目前对你保持明显戒备，聊天时会更冷淡、更尖锐，亲近举动也更容易被拒绝。"
    else:
        comment = f"{role_name} 还在观察你，聊天时会保持距离，但会根据你的表现逐渐改变措辞和态度。"

    msg = (
        f"📊 [玩家档案]\n"
        f"----------------\n"
        f"昵称: {user_name}\n"
        f"ID: {user_id}\n"
        f"当前看板娘: {role_name}\n"
        f"好感度: {fav} ({title})\n"
        f"互动次数: {count}\n"
        f"----------------\n"
        f"评价: {comment}"
    )
    await status_matcher.finish(msg)


help_matcher = on_command(
    "dice 帮助",
    priority=10,
)


@help_matcher.handle()
async def handle_help():
    msg = (
        "📖 [骰娘使用手册]\n"
        "----------------\n"
        "1. 投骰:\n"
        "   /dice 投骰                 随机 1-100\n"
        "   /dice 投骰 20              随机 1-20\n"
        "   /dice 投骰 100 60 侦查     进行一次带目标值的检定\n"
        "   .rd / .rd 20 / .rd 100 60 侦查\n\n"
        "2. 聊天:\n"
        "   私聊直接发送内容即可聊天。\n"
        "   群聊中 @我 后发送内容即可聊天。\n"
        "   聊天会记录互动次数，并可能轻微改变当前角色对你的好感度。\n"
        "   好感度影响聊天中的称呼、距离感、亲密度和语气，不影响骰点结果。\n\n"
        "3. 状态与角色:\n"
        "   /dice 状态                 查看当前角色、好感度和互动次数\n"
        "   /dice 角色列表             查看可用角色\n"
        "   /dice 切换角色 [key]       切换当前私聊或群聊的角色\n\n"
        "4. 管理指令:\n"
        "   /dice AI 状态\n"
        "   /dice AI 开启\n"
        "   /dice AI 关闭\n"
        "   /dice 设置好感 [user_id] [数值] [角色ID(可选)]"
    )
    await help_matcher.finish(msg)


ai_switch_matcher = on_regex(
    r"^/dice\s+ai(?:\s+.*)?$",
    flags=re.IGNORECASE,
    permission=SUPERUSER,
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


admin_matcher = on_command(
    "dice 设置好感",
    permission=SUPERUSER,
    priority=1,
)


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
    await admin_matcher.finish(f"✅ 已将用户 {target_id} 对角色【{target_role}】的好感度强制设定为 {val}。")


role_list_cmd = on_command(
    "dice 角色列表",
    priority=10,
)


@role_list_cmd.handle()
async def handle_role_list(bot: Bot, event: Event):
    roles = persona_manager.list_roles()
    current_data = persona_manager.get_persona(event)
    current_name = current_data.get("meta", {}).get("name", "未知")
    msg = f"当前环境扮演：{current_name}\n可用角色模板：\n" + "\n".join([f"- {r}" for r in roles])
    await role_list_cmd.finish(msg)


role_set_cmd = on_command(
    "dice 切换角色",
    priority=10,
)


@role_set_cmd.handle()
async def handle_role_set(bot: Bot, event: Event, args: Message = CommandArg()):
    role_key = args.extract_plain_text().strip()

    if not persona_manager.check_role_exists(role_key):
        await role_set_cmd.finish(f"❌ 找不到角色 key: {role_key}。请先用 /dice 角色列表 查看。")
        return

    role_name = persona_manager.get_role_name(role_key)

    if isinstance(event, GroupMessageEvent):
        if not (await SUPERUSER(bot, event) or await GROUP_ADMIN(bot, event) or await GROUP_OWNER(bot, event)):
            await role_set_cmd.finish("❌ 只有群主或管理员可以切换本群的看板娘哦。")
            return
        group_id = str(event.group_id)
        set_group_role(group_id, role_key)
        await role_set_cmd.finish(f"✨ 本群看板娘已切换为【{role_name}】！")

    elif isinstance(event, PrivateMessageEvent):
        user_id = event.get_user_id()
        set_private_role(user_id, role_key)
        memory_manager.clear(user_id)
        await role_set_cmd.finish(f"💗 你的私有伴侣已切换为【{role_name}】！\n(已重置短期记忆)")

    else:
        await role_set_cmd.finish(f"⚠️ Console 环境：已尝试切换为 {role_name}")
