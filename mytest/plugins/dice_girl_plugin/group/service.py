"""群聊主流程：记录 → 判断要不要开口 → 生成回复 → 发送并记录 → 更新档案。"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment

from ..config import config
from ..llm.group_service import generate_group_reply
from ..llm.prompts import build_group_prompt
from ..persona import persona_manager
from ..persona.lore import group_entries, match_lore
from ..storage import get_user_favorability, update_user_favorability
from ..storage import group_repository as repo
from . import context, members, profiles
from . import memory as group_memory
from .trigger import TopicLimiter, TriggerKind, decide

limiter = TopicLimiter(config.topic_cooldown_seconds, config.topic_replies_per_hour)
# 同一个群同时只处理一次回复，避免几个人同时 @ 时她并发回复多条、上下文错乱。
_group_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    settings = repo.get_group_chat_settings(group_id)

    member_scope = members.member_scope_for_group(group_id)
    display_name = members.record_speaker(group_id, user_id, event.sender)

    names = _known_names(member_scope, event)
    incoming = context.parse_group_event(event, names)
    incoming.display_name = display_name

    # 命令消息（/dice、.rd、p站 等）只交给命令处理，不进聊天上下文也不触发聊天。
    if incoming.text.startswith((".", "。", "/")):
        return

    context.store_incoming(incoming)
    role_id = persona_manager.get_current_role_id(event)
    group_memory.schedule_digest(group_id, persona_manager.get_role_name(role_id))
    if not settings.enabled:
        return

    role = persona_manager.get_role(role_id)
    lore = match_lore(
        group_entries(group_id) + persona_manager.lore_entries,
        incoming.text,
        role_id,
        "group",
    )
    decision = decide(
        text=incoming.text,
        at_me=incoming.at_me,
        reply_to_me=incoming.reply_to_me,
        role_aliases=persona_manager.get_role_aliases(role_id),
        topic_entries=lore,
        topic_reply_enabled=settings.topic_reply,
        limiter=limiter,
        group_id=group_id,
    )
    if decision.kind is TriggerKind.NONE:
        return

    async with _group_locks[group_id]:
        await _reply(bot, event, incoming, role_id, role, lore, decision, member_scope)


async def _reply(bot, event, incoming, role_id, role, lore, decision, member_scope) -> None:
    group_id = incoming.group_id
    user_id = incoming.user_id
    must_reply = decision.kind is TriggerKind.DIRECT
    if decision.kind is TriggerKind.TOPIC and decision.topic_entry:
        limiter.mark_checked(group_id, decision.topic_entry.entry_id)

    affinity_user = members.affinity_key(user_id, group_id)
    favorability = get_user_favorability(affinity_user, role_id=role_id)
    speaker_profile = profiles.describe(repo.get_member(member_scope, user_id), favorability)

    mentioned = members.find_mentioned_members(member_scope, incoming.text, exclude={user_id})
    for at_user in incoming.at_users:
        if at_user != user_id and at_user not in mentioned:
            mentioned.append(at_user)
    other_profiles = "\n\n".join(
        filter(None, (profiles.describe(repo.get_member(member_scope, uid)) for uid in mentioned[:3]))
    )

    # 上下文里最后一条就是当前消息，单独放进 current_message，避免重复。
    history = context.recent_context(group_id)
    if history and history[-1].message_id == incoming.message_id:
        history = history[:-1]

    # 用最近几句话一起去回忆，避免当前这句话太短（"那件事呢？"）找不到东西。
    recent_text = " ".join(m.content for m in history[-3:] if not m.is_bot)
    mentioned_names = [incoming.display_name] + [
        p.display_name for p in (repo.get_member(member_scope, uid) for uid in mentioned) if p
    ]
    recalled = await group_memory.recall(group_id, f"{recent_text} {incoming.text}".strip(), mentioned_names)
    group_summary = group_memory.format_memory(repo.get_group_digest(group_id), recalled)

    prompt = build_group_prompt(
        role,
        role_id=role_id,
        current_fav=favorability,
        speaker_name=incoming.display_name,
        speaker_id=user_id,
        speaker_profile=speaker_profile,
        other_profiles=other_profiles,
        transcript=context.format_transcript(history),
        current_text=incoming.rendered,
        trigger="direct" if must_reply else "topic",
        lore_entries=lore,
        group_summary=group_summary,
    )

    role_name = persona_manager.get_role_name(role_id)
    result = await generate_group_reply(
        prompt,
        must_reply=must_reply,
        speaker_name=incoming.display_name,
        role_name=role_name,
    )
    if result is None or not result.should_reply:
        logger.debug(f"[GroupChat] 群 {group_id} 选择不回复（{decision.reason}）")
        return

    await _send_replies(bot, event, result.replies, quote=must_reply and incoming.reply_to_user is None)
    for text in result.replies:
        context.store_bot_reply(group_id, str(event.self_id), role_name, text)
    if decision.kind is TriggerKind.TOPIC:
        limiter.mark_spoken(group_id)

    if result.delta:
        update_user_favorability(affinity_user, result.delta, role_id=role_id)
    profiles.apply_learned(
        member_scope,
        user_id,
        nicknames=result.learned.nicknames,
        facts=result.learned.facts,
        impression=result.learned.impression,
    )


async def _send_replies(bot: Bot, event: GroupMessageEvent, replies: list[str], *, quote: bool) -> None:
    for index, text in enumerate(replies):
        if index:
            # 按字数停顿一下，像人在打字；上限 4 秒。
            await asyncio.sleep(min(4.0, 0.8 + len(text) * 0.05 + random.uniform(0, 0.6)))
        message = Message()
        if index == 0 and quote:
            message += MessageSegment.reply(event.message_id)
        message += text
        await bot.send(event, message)


def _known_names(member_scope: str, event: GroupMessageEvent) -> dict[str, str]:
    """消息里 @ 到的人的显示名，用于把 @QQ号 渲染成 @昵称。"""
    names: dict[str, str] = {}
    for segment in event.original_message:
        if segment.type == "at":
            qq = str(segment.data.get("qq", ""))
            if qq and qq != "all":
                profile = repo.get_member(member_scope, qq)
                if profile and profile.display_name:
                    names[qq] = profile.display_name
    if event.reply and event.reply.sender.user_id is not None:
        sender = event.reply.sender
        names[str(sender.user_id)] = sender.card or sender.nickname or str(sender.user_id)
    return names
