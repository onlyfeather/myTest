from dataclasses import dataclass

from ...persona import persona_manager
from ...persona.lore import LoreEntry, format_lore, match_lore
from ...persona.schema import (
    PRIVATE,
    DutyState,
    SceneProfile,
    duty_state,
    examples_for,
    persona_for_scene,
    role_name,
    scene_profile,
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
    lore_context: str
    examples: str
    memory_summary: str
    scene: SceneProfile
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


def format_examples(raw_examples: list) -> str:
    if not raw_examples:
        return "- 无示例。"

    examples = []
    current = []
    for item in raw_examples:
        if item == "<START>":
            if current:
                examples.append("\n".join(current))
                current = []
            continue
        current.append(str(item))
    if current:
        examples.append("\n".join(current))

    if not examples:
        return "\n".join(str(item) for item in raw_examples)

    return "\n\n".join(examples[:2])


def relationship_directive_for(current_fav: int) -> str:
    """角色阶段里没有写 directive 时的通用兜底。"""
    if current_fav <= 20:
        return "保持明显距离和防备；称呼更冷，回复更短更尖锐；拒绝主动亲密。"
    if current_fav <= 59:
        return "可以正常对话，但仍保持审视和嘴硬，不要轻易承认喜欢或依赖。"
    if current_fav <= 85:
        return "明显更在意对方，允许别扭关心、吃醋、轻微暧昧和试探。"
    return "态度明显亲近，允许主动靠近和更柔软的称呼；仍保持角色核心性格，不要变成无条件顺从。"


def _role_id_of(role_data: dict) -> str:
    for role_id, role in persona_manager.data.get("roles", {}).items():
        if role is role_data:
            return role_id
    return ""


def build_character_context(
    role_data,
    current_fav: int,
    user_text: str = "",
    memory_summary: str = "",
    *,
    scene: str = PRIVATE,
    role_id: str = "",
    lore_entries: list[LoreEntry] | None = None,
) -> CharacterPromptContext:
    """组装角色信息。scene 决定是否带上 private_only 的设定和回复风格。

    lore_entries 为 None 时，用文件世界书按 user_text 自动匹配；
    群聊调用方会自己合并群梗后传进来。
    """
    meta = role_data.get("meta", {})
    persona = persona_for_scene(role_data, scene)
    stage = get_stage_config(role_data, current_fav)
    role_id = role_id or _role_id_of(role_data)
    duty = duty_state(role_data, scene, current_fav)
    if lore_entries is None:
        lore_entries = match_lore(persona_manager.lore_entries, user_text, role_id, scene)

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
        lore_context=format_lore(lore_entries),
        examples=format_examples(examples_for(role_data, duty)),
        memory_summary=memory_summary.strip() or "- 暂无长期记忆摘要。",
        scene=scene_profile(role_data, scene),
        duty=duty,
    )
