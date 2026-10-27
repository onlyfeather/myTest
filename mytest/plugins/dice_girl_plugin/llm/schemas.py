from pydantic import BaseModel, Field


class ChatReply(BaseModel):
    reply: str = Field(min_length=1)
    delta: int = Field(default=0, ge=-3, le=3)


class DiceReaction(BaseModel):
    reply: str = Field(min_length=1)
    delta: int = 0


class LearnedInfo(BaseModel):
    nicknames: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    impression: str = ""


class GroupReply(BaseModel):
    should_reply: bool = False
    replies: list[str] = Field(default_factory=list)
    delta: int = 0
    learned: LearnedInfo = Field(default_factory=LearnedInfo)


def parse_group_reply(data: dict) -> GroupReply:
    """宽松解析：类型不对的字段丢弃，数值截断到合法范围，不因为一个字段错误丢掉整条回复。"""
    replies = data.get("replies")
    if isinstance(replies, str):
        replies = [replies]
    if not isinstance(replies, list):
        replies = [data["reply"]] if isinstance(data.get("reply"), str) else []
    replies = [str(item).strip() for item in replies if str(item).strip()][:3]

    try:
        delta = max(-3, min(3, int(data.get("delta", 0))))
    except (TypeError, ValueError):
        delta = 0

    learned_raw = data.get("learned") if isinstance(data.get("learned"), dict) else {}

    def str_list(value, limit: int, max_len: int) -> list[str]:
        if not isinstance(value, list):
            return []
        items = [str(item).strip() for item in value if str(item).strip()]
        return [item[:max_len] for item in items][:limit]

    should_reply = data.get("should_reply")
    if not isinstance(should_reply, bool):
        should_reply = bool(replies)

    return GroupReply(
        should_reply=should_reply and bool(replies),
        replies=replies,
        delta=delta,
        learned=LearnedInfo(
            nicknames=str_list(learned_raw.get("nicknames"), 3, 16),
            facts=str_list(learned_raw.get("facts"), 3, 60),
            impression=str(learned_raw.get("impression") or "").strip()[:120],
        ),
    )


def validate_chat_reply(data: dict) -> dict:
    try:
        if hasattr(ChatReply, "model_validate"):
            reply = ChatReply.model_validate(data)
        else:
            reply = ChatReply.parse_obj(data)
    except AttributeError:
        reply = ChatReply.parse_obj(data)
    return reply.model_dump() if hasattr(reply, "model_dump") else reply.dict()


def validate_dice_reaction(data: dict) -> dict:
    try:
        if hasattr(DiceReaction, "model_validate"):
            reaction = DiceReaction.model_validate(data)
        else:
            reaction = DiceReaction.parse_obj(data)
    except AttributeError:
        reaction = DiceReaction.parse_obj(data)
    result = reaction.model_dump() if hasattr(reaction, "model_dump") else reaction.dict()
    result["delta"] = 0
    return result
