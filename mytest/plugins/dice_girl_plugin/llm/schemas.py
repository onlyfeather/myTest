from pydantic import BaseModel, Field, ValidationError


class ChatReply(BaseModel):
    reply: str = Field(min_length=1)
    delta: int = Field(default=0, ge=-3, le=3)


class DiceReaction(BaseModel):
    reply: str = Field(min_length=1)
    delta: int = 0


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
