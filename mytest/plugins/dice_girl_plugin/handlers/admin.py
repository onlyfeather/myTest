import re

from nonebot import on_command, on_regex
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.params import CommandArg, RegexStr
from nonebot.permission import SUPERUSER

from ..chat import memory_manager
from ..llm import is_ai_enabled, set_ai_enabled
from ..llm.summary_service import summarize_memory
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

memory_status_matcher = on_command(
    "dice 记忆状态",
    permission=private_superuser,
    priority=1,
)

memory_summary_matcher = on_command(
    "dice 摘要",
    permission=private_superuser,
    priority=1,
)

memory_clear_matcher = on_command(
    "dice 清空记忆",
    permission=private_superuser,
    priority=1,
)

memory_rebuild_matcher = on_command(
    "dice 重建摘要",
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


def _target_user_id(event: Event, args: Message) -> str:
    text = args.extract_plain_text().strip()
    return text.split()[0] if text else event.get_user_id()


def _target_role_id(event: Event, args: Message) -> str:
    parts = args.extract_plain_text().split()
    if len(parts) >= 2:
        return parts[1]
    return persona_manager.get_current_role_id(event)


@memory_status_matcher.handle()
async def handle_memory_status(event: Event, args: Message = CommandArg()):
    user_id = _target_user_id(event, args)
    role_id = _target_role_id(event, args)
    stats = memory_manager.scope_stats(user_id, role_id=role_id, scope=f"private:{user_id}")

    msg = (
        "🧠 [记忆状态]\n"
        f"用户: {stats['user_id']}\n"
        f"角色: {stats['role_id']}\n"
        f"作用域: {stats['scope']}\n"
        f"短期缓存: {stats['cached_messages']}/{stats['limit']}\n"
        f"持久消息: {stats['stored_messages']}\n"
        f"长期摘要: {'有' if stats['has_summary'] else '无'} ({stats['summary_chars']} 字)"
    )
    await memory_status_matcher.finish(msg)


@memory_summary_matcher.handle()
async def handle_memory_summary(event: Event, args: Message = CommandArg()):
    user_id = _target_user_id(event, args)
    role_id = _target_role_id(event, args)
    summary = memory_manager.get_summary(user_id, role_id=role_id, scope=f"private:{user_id}")
    if not summary:
        summary = "暂无长期摘要。"
    await memory_summary_matcher.finish(f"🧠 [长期摘要]\n用户: {user_id}\n角色: {role_id}\n\n{summary}")


@memory_clear_matcher.handle()
async def handle_memory_clear(event: Event, args: Message = CommandArg()):
    user_id = _target_user_id(event, args)
    role_id = _target_role_id(event, args)
    removed = memory_manager.clear(user_id, role_id=role_id, scope=f"private:{user_id}")
    await memory_clear_matcher.finish(
        f"✅ 已清空用户 {user_id} 在角色【{role_id}】下的私聊记忆。清理记录数: {removed}"
    )


@memory_rebuild_matcher.handle()
async def handle_memory_rebuild(event: Event, args: Message = CommandArg()):
    user_id = _target_user_id(event, args)
    role_id = _target_role_id(event, args)
    history = memory_manager.get_history(user_id, role_id=role_id, scope=f"private:{user_id}")
    old_summary = memory_manager.get_summary(user_id, role_id=role_id, scope=f"private:{user_id}")

    summary = await summarize_memory(old_summary, history)
    if not summary:
        await memory_rebuild_matcher.finish("❌ 摘要重建失败：AI 不可用或当前没有可摘要内容。")
        return

    memory_manager.set_summary(
        user_id,
        summary,
        role_id=role_id,
        scope=f"private:{user_id}",
    )
    await memory_rebuild_matcher.finish(
        f"✅ 已重建用户 {user_id} 在角色【{role_id}】下的长期摘要。\n\n{summary}"
    )
