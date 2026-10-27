"""群员识别：一个人可能有多个称呼（群名片、QQ 昵称、别人给起的外号）。"""

from __future__ import annotations

import re
import time

from ..config import config
from ..storage import group_repository as repo


def member_scope_for_group(group_id: str) -> str:
    return "global" if config.member_scope == "global" else f"group:{group_id}"


def member_scope_for_private() -> str | None:
    """私聊时使用的档案作用域；按群隔离时私聊不读写任何群的档案。"""
    return "global" if config.member_scope == "global" else None


def affinity_key(user_id: str, group_id: str | None) -> str:
    """好感度表里的 user_id 键。按群隔离时加群前缀，全局模式保持原样（兼容旧数据）。"""
    if group_id and config.affinity_scope == "group":
        return f"group:{group_id}|{user_id}"
    return user_id


def sender_names(sender) -> dict[str, str]:
    names = {}
    card = (getattr(sender, "card", "") or "").strip()
    nickname = (getattr(sender, "nickname", "") or "").strip()
    if card:
        names["card"] = card
    if nickname and nickname != card:
        names["nickname"] = nickname
    return names


def display_name_of(sender) -> str:
    names = sender_names(sender)
    return names.get("card") or names.get("nickname") or "群友"


def record_speaker(group_id: str, user_id: str, sender, now: float | None = None) -> str:
    """登记一次发言，返回此人的显示名。"""
    display_name = display_name_of(sender)
    repo.touch_member(
        member_scope_for_group(group_id),
        user_id,
        display_name,
        now or time.time(),
        sender_names(sender),
    )
    return display_name


def find_mentioned_members(member_scope: str, text: str, exclude: set[str]) -> list[str]:
    """找出文本里用昵称提到的群员（不含 @，@ 由消息段直接识别）。

    为避免误判：昵称至少 2 个字符；英文/数字昵称按整词匹配。
    """
    if not text:
        return []
    lowered = text.casefold()
    found: list[str] = []
    for alias_key, user_ids in repo.load_alias_index(member_scope).items():
        if len(alias_key) < 2 or not _contains(lowered, alias_key):
            continue
        for user_id in user_ids:
            if user_id not in exclude and user_id not in found:
                found.append(user_id)
    return found


def _contains(text: str, keyword: str) -> bool:
    if re.fullmatch(r"[0-9a-z_ .\-]+", keyword):
        return re.search(rf"(?<![0-9a-z_]){re.escape(keyword)}(?![0-9a-z_])", text) is not None
    return keyword in text
