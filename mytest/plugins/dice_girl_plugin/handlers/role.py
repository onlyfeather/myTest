from nonebot import on_command
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from ..persona import persona_manager
from ..storage import set_group_role, set_private_role

role_list_cmd = on_command(
    "dice 角色列表",
    priority=10,
)

role_set_cmd = on_command(
    "dice 切换角色",
    priority=10,
)


@role_list_cmd.handle()
async def handle_role_list(bot: Bot, event: Event):
    roles = persona_manager.list_roles()
    current_data = persona_manager.get_persona(event)
    current_name = current_data.get("meta", {}).get("name", "未知")
    msg = f"当前环境扮演：{current_name}\n可用角色模板：\n" + "\n".join(
        [f"- {role}" for role in roles]
    )
    await role_list_cmd.finish(msg)


@role_set_cmd.handle()
async def handle_role_set(bot: Bot, event: Event, args: Message = CommandArg()):
    role_key = args.extract_plain_text().strip()

    if not persona_manager.check_role_exists(role_key):
        await role_set_cmd.finish(f"❌ 找不到角色 key: {role_key}。请先用 /dice 角色列表 查看。")
        return

    role_name = persona_manager.get_role_name(role_key)

    if isinstance(event, GroupMessageEvent):
        if not (
            await SUPERUSER(bot, event)
            or await GROUP_ADMIN(bot, event)
            or await GROUP_OWNER(bot, event)
        ):
            await role_set_cmd.finish("❌ 只有群主或管理员可以切换本群的看板娘哦。")
            return
        set_group_role(str(event.group_id), role_key)
        await role_set_cmd.finish(f"✨ 本群的投骰点评角色已切换为【{role_name}】！")

    elif isinstance(event, PrivateMessageEvent):
        set_private_role(event.get_user_id(), role_key)
        await role_set_cmd.finish(f"✨ 你的投骰点评角色已切换为【{role_name}】！")

    else:
        await role_set_cmd.finish(f"⚠️ Console 环境：已尝试切换为 {role_name}")
