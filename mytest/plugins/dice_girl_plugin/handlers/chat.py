from nonebot import logger, on_message
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

from ..chat import memory_manager
from ..llm import analyze_chat
from ..persona import persona_manager
from ..storage import get_user_favorability, update_user_favorability
from .common import extract_plain_text, get_user_name


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


chat_matcher = on_message(rule=chat_rule_check, priority=99, block=False)


@chat_matcher.handle()
async def handle_chat(bot: Bot, event: Event):
    user_id = event.get_user_id()
    user_name = get_user_name(event)
    role_key = persona_manager.get_current_role_id(event)

    text = extract_plain_text(event).strip()

    if not text:
        return

    current_fav = get_user_favorability(user_id, role_id=role_key)
    history = memory_manager.get_history(user_id, role_id=role_key, event=event)
    memory_summary = memory_manager.get_summary(user_id, role_id=role_key, event=event)

    ai_result = await analyze_chat(
        current_fav,
        text,
        user_name,
        history,
        memory_summary=memory_summary,
        event=event,
    )
    logger.debug("[Chat] AI response received")

    reply_text = ai_result.get("reply", "")
    if not reply_text:
        reply_text = "..."

    delta = ai_result.get("delta", 0)
    update_user_favorability(user_id, delta, role_id=role_key)

    memory_manager.add_message(user_id, "user", text, role_id=role_key, event=event)
    memory_manager.add_message(
        user_id,
        "assistant",
        reply_text,
        role_id=role_key,
        event=event,
    )

    await chat_matcher.finish(reply_text)
