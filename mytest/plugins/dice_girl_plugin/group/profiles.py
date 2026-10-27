"""群员档案：给 prompt 用的文字描述，以及把模型学到的信息写回档案。"""

from __future__ import annotations

import time

from nonebot import logger

from ..storage import group_repository as repo
from ..storage.group_repository import MemberProfile

MAX_FACTS = 20
MAX_ALIASES_SHOWN = 5


def describe(profile: MemberProfile | None, favorability: int | None = None) -> str:
    if profile is None:
        return ""
    lines = [f"- 显示名：{profile.display_name or profile.user_id}"]
    aliases = [a for a in profile.aliases if a != profile.display_name][:MAX_ALIASES_SHOWN]
    if aliases:
        lines.append(f"- 也被叫作：{'、'.join(aliases)}")
    if favorability is not None:
        lines.append(f"- 你对他的好感：{favorability}/100")
    if profile.impression:
        lines.append(f"- 你对他的印象：{profile.impression}")
    if profile.facts:
        lines.append("- 你记得的事：")
        lines.extend(f"  - {fact}" for fact in profile.facts[-8:])
    if profile.message_count:
        lines.append(f"- 在这里说过约 {profile.message_count} 句话")
    return "\n".join(lines)


def apply_learned(
    member_scope: str,
    user_id: str,
    *,
    nicknames: list[str],
    facts: list[str],
    impression: str,
) -> None:
    """合并模型从这条消息里学到的东西。去重，事实只追加，超过上限丢最旧的。"""
    if not (nicknames or facts or impression):
        return
    now = time.time()
    for nickname in nicknames:
        repo.add_member_alias(member_scope, user_id, nickname, "learned", now)

    profile = repo.get_member(member_scope, user_id)
    existing = profile.facts if profile else []
    known = {fact.casefold() for fact in existing}
    merged = list(existing)
    for fact in facts:
        if fact.casefold() not in known:
            merged.append(fact)
            known.add(fact.casefold())
    merged = merged[-MAX_FACTS:]

    repo.update_member_profile(
        member_scope,
        user_id,
        impression=impression or None,
        facts=merged if merged != existing else None,
        now=now,
    )
    logger.debug(
        f"[Members] 更新 {member_scope}/{user_id}: +{len(nicknames)} 昵称, "
        f"+{len(merged) - len(existing)} 事实, 印象{'已更新' if impression else '未变'}"
    )
