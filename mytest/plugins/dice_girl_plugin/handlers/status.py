from nonebot import on_command
from nonebot.adapters import Bot, Event

from ..persona import persona_manager
from ..storage import get_user_stats
from .common import get_user_name

status_matcher = on_command(
    "dice 状态",
    priority=10,
)

help_matcher = on_command(
    "dice 帮助",
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
        "   管理指令仅 SUPERUSER 私聊可用。\n"
        "   /dice AI 状态\n"
        "   /dice AI 开启\n"
        "   /dice AI 关闭\n"
        "   /dice 设置好感 [user_id] [数值] [角色ID(可选)]\n"
        "   /dice 记忆状态 [user_id(可选)] [角色ID(可选)]\n"
        "   /dice 摘要 [user_id(可选)] [角色ID(可选)]\n"
        "   /dice 清空记忆 [user_id(可选)] [角色ID(可选)]\n"
        "   /dice 重建摘要 [user_id(可选)] [角色ID(可选)]"
    )
    await help_matcher.finish(msg)
