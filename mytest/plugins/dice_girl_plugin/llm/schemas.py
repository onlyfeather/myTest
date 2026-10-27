from pydantic import BaseModel, Field


class DiceReaction(BaseModel):
    reply: str = Field(min_length=1)
    delta: int = 0


def validate_dice_reaction(data: dict) -> dict:
    result = DiceReaction.model_validate(data).model_dump()
    result["delta"] = 0
    return result
