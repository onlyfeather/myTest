from nonebot import on_command
from nonebot.adapters import Bot, Event

from ..llm.prompts import get_stage_config
from ..persona import persona_manager
from ..storage import get_user_favorability
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
    role_key = persona_manager.get_current_role_id(event)
    role = persona_manager.get_role(role_key)
    role_name = persona_manager.get_role_name(role_key)

    fav = get_user_favorability(user_id, role_id=role_key)
    stage = get_stage_config(role, fav)

    msg = (
        f"📊 [玩家档案]\n"
        f"----------------\n"
        f"昵称: {get_user_name(event)}\n"
        f"ID: {user_id}\n"
        f"角色: {role_name}\n"
        f"好感度: {fav}（{stage.get('name', '未知')}）\n"
        f"----------------\n"
        f"好感度只影响{role_name}点评投骰时的称呼和语气，不影响骰点结果。"
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
        "2. 状态:\n"
        "   /dice 状态                 查看好感度\n\n"
        "3. 管理指令（仅 SUPERUSER 私聊可用）:\n"
        "   /dice AI 状态|开启|关闭     投骰点评的 AI 开关\n"
        "   /dice 设置好感 [user_id] [数值] [角色ID(可选)]"
    )
    await help_matcher.finish(msg)
