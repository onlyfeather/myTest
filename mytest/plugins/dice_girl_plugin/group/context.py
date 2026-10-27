"""把 OneBot 消息转成群聊上下文里的一行文字。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

from ..config import config
from ..storage import group_repository as repo
from ..storage.group_repository import GroupMessage

_SEGMENT_PLACEHOLDERS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情包]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[聊天记录]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
    "poke": "[戳一戳]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
}


@dataclass
class IncomingMessage:
    """一条群消息被解析后的结果。"""

    group_id: str
    user_id: str
    display_name: str
    text: str  # 用于判断触发和匹配世界书的纯文本（@ 已替换成昵称）
    rendered: str  # 存进上下文的完整文本（含 [图片] 等占位符）
    at_me: bool
    reply_to_me: bool
    reply_to_user: str | None
    at_users: list[str]
    message_id: str | None


def render_message(message: Message, self_id: str, names: dict[str, str]) -> tuple[str, list[str]]:
    """把消息段转成文字，返回 (文本, 被 @ 的 QQ 号列表)。"""
    parts: list[str] = []
    at_users: list[str] = []
    for segment in message:
        if segment.type == "text":
            parts.append(segment.data.get("text", ""))
        elif segment.type == "at":
            qq = str(segment.data.get("qq", ""))
            if qq == "all":
                parts.append("@全体成员")
            elif qq == self_id:
                continue
            else:
                at_users.append(qq)
                parts.append(f"@{names.get(qq) or segment.data.get('name') or qq}")
        else:
            placeholder = _SEGMENT_PLACEHOLDERS.get(segment.type)
            if placeholder:
                parts.append(placeholder)
    return " ".join("".join(parts).split()), at_users


def parse_group_event(event: GroupMessageEvent, names: dict[str, str]) -> IncomingMessage:
    self_id = str(event.self_id)
    # original_message 保留了被适配器剥掉的开头 @，用于判断有没有 @ 她。
    at_me = any(
        seg.type == "at" and str(seg.data.get("qq")) == self_id for seg in event.original_message
    )
    reply_to_user = None
    reply_to_me = False
    if event.reply and event.reply.sender.user_id is not None:
        reply_to_user = str(event.reply.sender.user_id)
        reply_to_me = reply_to_user == self_id

    text, at_users = render_message(event.message, self_id, names)
    rendered = text
    if reply_to_user and not reply_to_me:
        rendered = f"(回复 {names.get(reply_to_user, reply_to_user)}) {text}"

    return IncomingMessage(
        group_id=str(event.group_id),
        user_id=str(event.user_id),
        display_name="",
        text=text,
        rendered=rendered or "[空消息]",
        at_me=at_me,
        reply_to_me=reply_to_me,
        reply_to_user=reply_to_user,
        at_users=at_users,
        message_id=str(event.message_id),
    )


def store_incoming(message: IncomingMessage) -> None:
    repo.append_group_message(
        message.group_id,
        GroupMessage(
            user_id=message.user_id,
            display_name=message.display_name,
            content=message.rendered,
            is_bot=False,
            created_at=time.time(),
            reply_to_user=message.reply_to_user,
            message_id=message.message_id,
        ),
        keep_limit=config.group_keep_messages,
        retention_seconds=config.group_retention_hours * 3600,
    )


def store_bot_reply(group_id: str, bot_id: str, role_name: str, content: str) -> None:
    repo.append_group_message(
        group_id,
        GroupMessage(
            user_id=bot_id,
            display_name=role_name,
            content=content,
            is_bot=True,
            created_at=time.time(),
        ),
        keep_limit=config.group_keep_messages,
        retention_seconds=config.group_retention_hours * 3600,
    )


def recent_context(group_id: str) -> list[GroupMessage]:
    return repo.load_group_messages(group_id, config.group_context_size)


def format_transcript(messages: list[GroupMessage], now: float | None = None) -> str:
    now = now or time.time()
    lines = []
    for item in messages:
        speaker = f"{item.display_name}(你)" if item.is_bot else f"{item.display_name}#{item.user_id[-4:]}"
        lines.append(f"[{_ago(now - item.created_at)}] {speaker}: {item.content}")
    return "\n".join(lines) or "(群里暂时没有消息)"


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60}分钟前"
    if seconds < 86400:
        return f"{seconds // 3600}小时前"
    return f"{seconds // 86400}天前"
