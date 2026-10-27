from nonebot import logger, on_message
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

from ..chat import memory_manager
from ..group import members, profiles
from ..group.service import handle_group_message
from ..llm import analyze_chat
from ..persona import persona_manager
from ..storage import get_user_favorability, update_user_favorability
from ..storage import group_repository as repo
from .common import extract_plain_text, get_user_name


async def private_chat_rule(event: Event) -> bool:
    if not isinstance(event, PrivateMessageEvent):
        return False
    return not extract_plain_text(event).startswith((".", "。", "/"))


async def group_message_rule(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent)


# 群消息全部经过这里：先记进上下文，再判断要不要开口。block=False，不影响其他插件的命令。
group_matcher = on_message(rule=group_message_rule, priority=98, block=False)
private_matcher = on_message(rule=private_chat_rule, priority=99, block=False)


@group_matcher.handle()
async def handle_group(bot: OneBot, event: GroupMessageEvent):
    try:
        await handle_group_message(bot, event)
    except Exception:
        logger.exception(f"[GroupChat] 处理群 {event.group_id} 的消息失败")


@private_matcher.handle()
async def handle_private(bot: Bot, event: PrivateMessageEvent):
    user_id = event.get_user_id()
    user_name = get_user_name(event)
    role_key = persona_manager.get_current_role_id(event)

    text = extract_plain_text(event).strip()
    if not text:
        return

    current_fav = get_user_favorability(user_id, role_id=role_key)
    history = memory_manager.get_history(user_id, role_id=role_key, event=event)
    memory_summary = memory_manager.get_summary(user_id, role_id=role_key, event=event)

    # 档案按群隔离时，私聊不读取任何群里形成的印象；全局模式下共享同一份。
    member_profile = ""
    scope = members.member_scope_for_private()
    if scope:
        member_profile = profiles.describe(repo.get_member(scope, user_id))

    ai_result = await analyze_chat(
        current_fav,
        text,
        user_name,
        history,
        memory_summary=memory_summary,
        member_profile=member_profile,
        event=event,
    )
    logger.debug("[Chat] AI response received")

    reply_text = ai_result.get("reply", "") or "..."
    update_user_favorability(user_id, ai_result.get("delta", 0), role_id=role_key)

    memory_manager.add_message(user_id, "user", text, role_id=role_key, event=event)
    memory_manager.add_message(user_id, "assistant", reply_text, role_id=role_key, event=event)

    await private_matcher.finish(reply_text)
