from dataclasses import dataclass

from nonebot import logger

from ...persona import persona_manager


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


def get_stage_config(role_data, current_fav):
    if "stages" not in role_data:
        return {}
    for stage in role_data["stages"]:
        min_v, max_v = stage["range"]
        if min_v <= current_fav <= max_v:
            return stage
    return role_data["stages"][0]


def format_prompt_items(items: list) -> str:
    if not items:
        return "- 未特别设定。"
    return "\n".join(f"- {item}" for item in items if item)


def format_prompt_mapping(items: dict) -> str:
    if not items:
        return "- 未特别设定。"
    return "\n".join(f"- {key}: {value}" for key, value in items.items() if value)


def extract_lore(lore_list: list, user_text: str) -> str:
    if not lore_list or not user_text:
        return "- 无触发。"

    triggered_lore = []
    text_lower = user_text.lower()

    for entry in lore_list:
        keys = entry.get("keys", [])
        content = entry.get("content", "")
        for key in keys:
            if key.lower() in text_lower:
                triggered_lore.append(content)
                break
        if len(triggered_lore) >= 5:
            break

    if not triggered_lore:
        return "- 无触发。"

    logger.debug(f"[Lore] Triggered {len(triggered_lore)} lore entries")
    return "\n".join(f"- {item}" for item in triggered_lore)


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
    if current_fav <= 20:
        return (
            "保持明显距离和防备；称呼更冷，回复更短更尖锐；拒绝主动亲密。"
            "若用户试图亲近，先表现戒备、躲避或冷淡拒绝。"
        )
    if current_fav <= 59:
        return (
            "可以正常对话，但仍保持审视和嘴硬；关心必须包装成吐槽、"
            "职责或风险控制，不要轻易承认喜欢或依赖。"
        )
    if current_fav <= 85:
        return (
            "明显更在意用户，允许别扭关心、吃醋、轻微暧昧和试探；"
            "被夸奖或靠近时要有害羞、停顿、嘴硬等反应。"
        )
    return (
        "态度明显亲近，允许主动靠近、占有欲、撒娇式试探和更柔软的称呼；"
        "仍保持角色核心性格，不要变成无条件顺从。"
    )


def build_character_context(
    role_data,
    current_fav: int,
    user_text: str = "",
    memory_summary: str = "",
) -> CharacterPromptContext:
    meta = role_data.get("meta", {})
    persona = role_data.get("persona", {})
    role_name = meta.get("name", role_data.get("name", "骰娘"))
    stage = get_stage_config(role_data, current_fav)

    return CharacterPromptContext(
        role_name=role_name,
        description=persona.get("description", role_data.get("description", "")),
        appearance=persona.get("appearance", "外貌未定义"),
        voice_style=persona.get("voice_style", "说话风格未定义"),
        core_conflict=persona.get("core_conflict", ""),
        behavior_habits=format_prompt_items(persona.get("behavior_habits", [])),
        emotional_triggers=format_prompt_mapping(
            persona.get("emotional_triggers", {})
        ),
        intimacy_boundaries=format_prompt_items(
            persona.get("intimacy_boundaries", [])
        ),
        speech_constraints=format_prompt_items(persona.get("speech_constraints", [])),
        current_fav=current_fav,
        stage_name=stage.get("name", "未知阶段"),
        user_call=stage.get("user_call", persona.get("user_call_default", "用户")),
        attitude=stage.get("attitude", "态度未定义"),
        relationship_directive=relationship_directive_for(current_fav),
        lore_context=extract_lore(persona_manager.get_global_lore(), user_text),
        examples=format_examples(role_data.get("examples", [])),
        memory_summary=memory_summary.strip() or "- 暂无长期记忆摘要。",
    )


def build_dynamic_prompt(
    role_data,
    current_fav,
    user_name,
    user_text="",
    is_chat=True,
):
    if is_chat:
        from .chat import build_chat_prompt

        return build_chat_prompt(role_data, current_fav, user_name, user_text)

    from .dice import build_dice_prompt

    return build_dice_prompt(role_data, current_fav, user_name, user_text)
