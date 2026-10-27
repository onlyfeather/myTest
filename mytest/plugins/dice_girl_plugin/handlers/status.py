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
async def handle_help(bot: Bot, event: Event):
    # 旧指令保留，内容和 /帮助 投骰 一致。
    from ..._shared.events import is_superuser
    from ..._shared.help import help_registry

    topic = help_registry.find("dice")
    msg = help_registry.detail(topic, is_admin=await is_superuser(bot, event)) if topic else "发送 /帮助 查看指令"
    await help_matcher.finish(msg + "\n\n其他功能发送 /帮助 查看。")
