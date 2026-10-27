from dataclasses import dataclass

from ...persona.schema import (
    PRIVATE,
    DutyState,
    duty_state,
    persona_for_scene,
    role_name,
    user_call_for,
)


@dataclass
class CharacterPromptContext:
    role_name: str
    description: str
    appearance: str
    voice_style: str
    core_conflict: str
    behavior_habits: str
    emotional_triggers: str
    intimacy_boundaries: str
    speech_constraints: str
    current_fav: int
    stage_name: str
    user_call: str
    attitude: str
    relationship_directive: str
    duty: DutyState | None = None


def get_stage_config(role_data, current_fav):
    stages = role_data.get("stages") or []
    for stage in stages:
        min_v, max_v = stage["range"]
        if min_v <= current_fav <= max_v:
            return stage
    return stages[0] if stages else {}


def format_prompt_items(items: list) -> str:
    if not items:
        return "- 未特别设定。"
    return "\n".join(f"- {item}" for item in items if item)


def format_prompt_mapping(items: dict) -> str:
    if not items:
        return "- 未特别设定。"
    return "\n".join(f"- {key}: {value}" for key, value in items.items() if value)


def relationship_directive_for(current_fav: int) -> str:
    """角色阶段里没有写 directive 时的通用兜底。"""
    if current_fav <= 20:
        return "保持明显距离和防备；称呼更冷，回复更短更尖锐；拒绝主动亲密。"
    if current_fav <= 59:
        return "可以正常对话，但仍保持审视和嘴硬，不要轻易承认喜欢或依赖。"
    if current_fav <= 85:
        return "明显更在意对方，允许别扭关心、吃醋、轻微暧昧和试探。"
    return "态度明显亲近，允许主动靠近和更柔软的称呼；仍保持角色核心性格，不要变成无条件顺从。"


def build_character_context(
    role_data,
    current_fav: int,
    *,
    scene: str = PRIVATE,
) -> CharacterPromptContext:
    """组装角色信息。scene 决定是否带上 private_only 的设定，以及上班 / 下班状态。"""
    meta = role_data.get("meta", {})
    persona = persona_for_scene(role_data, scene)
    stage = get_stage_config(role_data, current_fav)
    duty = duty_state(role_data, scene, current_fav)

    return CharacterPromptContext(
        role_name=role_name(role_data),
        description=persona.get("description", role_data.get("description", "")),
        appearance=persona.get("appearance", "外貌未定义"),
        voice_style=persona.get("voice_style", "说话风格未定义"),
        core_conflict=persona.get("core_conflict", ""),
        behavior_habits=format_prompt_items(persona.get("behavior_habits", [])),
        emotional_triggers=format_prompt_mapping(persona.get("emotional_triggers", {})),
        intimacy_boundaries=format_prompt_items(persona.get("intimacy_boundaries", [])),
        speech_constraints=format_prompt_items(persona.get("speech_constraints", [])),
        current_fav=current_fav,
        stage_name=stage.get("name", "未知阶段"),
        user_call=user_call_for(
            stage, duty, persona.get("user_call_default", meta.get("user_call", "用户"))
        ),
        attitude=stage.get("attitude", "态度未定义"),
        relationship_directive=stage.get("directive") or relationship_directive_for(current_fav),
        duty=duty,
    )
