from .common import CharacterPromptContext


def _duty_block(ctx: CharacterPromptContext) -> str:
    if ctx.duty is None:
        return ""
    rules = "\n".join(f"- {rule}" for rule in ctx.duty.rules) or "- 无"
    return (
        "\n<duty_state>\n"
        f"current: {ctx.duty.label}\n"
        f"{ctx.duty.description}\n"
        f"{rules}\n"
        "</duty_state>"
    )


def render_character_block(ctx: CharacterPromptContext) -> str:
    return f"""
<character>
name: {ctx.role_name}
appearance: {ctx.appearance}
personality: {ctx.description}
voice_style: {ctx.voice_style}
core_conflict: {ctx.core_conflict}
</character>

<behavior_habits>
{ctx.behavior_habits}
</behavior_habits>

<emotional_triggers>
{ctx.emotional_triggers}
</emotional_triggers>

<relationship_state>
favorability: {ctx.current_fav}
stage: {ctx.stage_name}
user_call: {ctx.user_call}
attitude: {ctx.attitude}
directive: {ctx.relationship_directive}
</relationship_state>
{_duty_block(ctx)}

<boundaries>
{ctx.intimacy_boundaries}
</boundaries>

<speech_constraints>
{ctx.speech_constraints}
</speech_constraints>
""".strip()
