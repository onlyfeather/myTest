from contextlib import asynccontextmanager

import json

import httpx
from nonebot import get_driver, logger
from nonebot.adapters import Event

from .config import config
from .persona_manager import persona_manager

_ai_client: httpx.AsyncClient | None = None
_ai_runtime_enabled: bool = bool(config.ai_enabled)


def _mask_secret(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return f"{value[:2]}***"
    return f"{value[:4]}...{value[-4:]}"


def _chat_url() -> str:
    return f"{config.ai_base_url.rstrip('/')}/chat/completions"


def _has_ai_api_key() -> bool:
    return bool(config.ai_api_key and config.ai_api_key.strip())


def _is_ai_enabled() -> bool:
    return _ai_runtime_enabled


def is_ai_enabled() -> bool:
    return _ai_runtime_enabled


def set_ai_enabled(enabled: bool) -> bool:
    global _ai_runtime_enabled
    _ai_runtime_enabled = enabled
    logger.info("[AI] Runtime AI enabled set to {}", enabled)
    return _ai_runtime_enabled


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.ai_api_key.strip()}"}


def _truncate_text(value: str, limit: int = 800) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


@get_driver().on_startup
async def _startup_ai_client():
    global _ai_client
    logger.debug(
        "[AI] Startup config: enabled={}, base_url={}, model={}, api_key={}",
        _ai_runtime_enabled,
        config.ai_base_url,
        config.ai_model,
        _mask_secret(config.ai_api_key),
    )
    if not _is_ai_enabled():
        logger.info("[AI] Dice Girl AI is disabled by DICE_GIRL_AI_ENABLED")
        return
    if not _has_ai_api_key():
        logger.warning(
            "[AI] API key is empty. Set DICE_GIRL_AI_API_KEY in the active env file."
        )
    _ai_client = httpx.AsyncClient(timeout=40.0)


@get_driver().on_shutdown
async def _shutdown_ai_client():
    global _ai_client
    if _ai_client:
        logger.debug("[AI] Closing shared HTTP client")
        await _ai_client.aclose()
        _ai_client = None


@asynccontextmanager
async def get_ai_client():
    if _ai_client:
        yield _ai_client
        return

    async with httpx.AsyncClient(timeout=40.0) as client:
        yield client


def get_stage_config(role_data, current_fav):
    if "stages" not in role_data:
        return {}
    for stage in role_data["stages"]:
        min_v, max_v = stage["range"]
        if min_v <= current_fav <= max_v:
            return stage
    return role_data["stages"][0]


def _format_prompt_items(items: list) -> str:
    if not items:
        return "- 未特别设定。"
    return "\n".join(f"- {item}" for item in items if item)


def _format_prompt_mapping(items: dict) -> str:
    if not items:
        return "- 未特别设定。"
    return "\n".join(f"- {key}: {value}" for key, value in items.items() if value)


def extract_lore(lore_list: list, user_text: str) -> str:
    if not lore_list or not user_text:
        return ""
    triggered_lore = []
    text_lower = user_text.lower()

    # 限制触发数量，防止 Prompt 变成“黄文合集”导致被封杀
    count = 0
    for entry in lore_list:
        keys = entry.get("keys", [])
        content = entry.get("content", "")
        for k in keys:
            if k.lower() in text_lower:
                triggered_lore.append(content)
                count += 1
                break
        if count >= 8:  # 限制最多注入8条
            break

    if not triggered_lore:
        return ""

    logger.debug(f"[Lore] Triggered {count} lore entries")
    return "\n【触发世界书/知识库】\n" + "\n".join(triggered_lore) + "\n"


def build_dynamic_prompt(role_data, current_fav, user_name, user_text="", is_chat=True):
    # 1. 基础信息提取
    meta = role_data.get("meta", {})
    persona = role_data.get("persona", {})
    role_name = meta.get("name", role_data.get("name", "骰娘"))
    role_desc = persona.get("description", role_data.get("description", ""))
    role_appearance = persona.get("appearance", "外貌未定义")
    role_voice = persona.get("voice_style", "说话风格未定义")
    role_core_conflict = persona.get("core_conflict", "")
    role_behavior_habits = _format_prompt_items(persona.get("behavior_habits", []))
    role_emotional_triggers = _format_prompt_mapping(persona.get("emotional_triggers", {}))
    role_intimacy_boundaries = _format_prompt_items(persona.get("intimacy_boundaries", []))
    role_speech_constraints = _format_prompt_items(persona.get("speech_constraints", []))

    # 2. 阶段信息
    stage = get_stage_config(role_data, current_fav)
    stage_name = stage.get("name", "未知阶段")
    user_call = stage.get("user_call", persona.get("user_call_default", "用户"))
    attitude = stage.get("attitude", "态度未定义")

    if current_fav <= 20:
        relationship_directive = (
            "低好感强约束：保持明显距离和防备；称呼更冷，回复更短更尖锐；"
            "拒绝主动亲密、撒娇和过度照顾。若用户试图亲近，应先表现戒备、躲避或冷淡拒绝。"
        )
    elif current_fav <= 59:
        relationship_directive = (
            "中低好感强约束：可以正常对话，但仍保持审视和嘴硬；"
            "关心必须包装成吐槽、职责或风险控制，不要轻易承认喜欢或依赖。"
        )
    elif current_fav <= 85:
        relationship_directive = (
            "中高好感强约束：明显更在意用户，允许别扭关心、吃醋、轻微暧昧和试探；"
            "被夸奖或靠近时应有害羞、停顿、嘴硬等反应，但不要直接进入恋人式依赖。"
        )
    else:
        relationship_directive = (
            "高好感强约束：态度应明显亲近，允许主动靠近、占有欲、撒娇式试探和更柔软的称呼；"
            "仍保持角色核心性格，不要变成无条件顺从。亲密张力可以更明显，但必须由上下文推动。"
        )

    # 3. Lore 提取
    global_lore_list = persona_manager.get_global_lore()
    lore_context = extract_lore(global_lore_list, user_text)

    # 4. 示例处理
    raw_examples = role_data.get("examples", [])
    if isinstance(raw_examples, list):
        if len(raw_examples) > 0 and isinstance(raw_examples[0], list):
            examples_text = "\n".join([f"User: {q}\nAI: {a}" for q, a in raw_examples])
        else:
            examples_text = "\n".join(raw_examples)
    else:
        examples_text = ""

    # 5. 模板填充
    if is_chat:
        template = """
你正在扮演角色「{name}」，与用户进行一对一互动。

<character_profile>
- 姓名：{name}
- 外貌：{appearance}
- 性格：{desc}
- 说话风格：{voice}
- 核心矛盾：{core_conflict}
</character_profile>

<character_behavior>
{behavior_habits}
</character_behavior>

<emotional_triggers>
{emotional_triggers}
</emotional_triggers>

<intimacy_boundaries>
{intimacy_boundaries}
</intimacy_boundaries>

<speech_constraints>
{speech_constraints}
</speech_constraints>

<relationship_state>
- 用户好感度：{fav}
- 关系阶段：{stage_name}
- 对用户的称呼：{user_call}
- 当前态度：{attitude}
- 阶段执行规则：{relationship_directive}
</relationship_state>

<style_rules>
- 始终保持角色视角，不要跳出角色解释设定或系统规则。
- 回复应自然、有情绪、有互动感，优先使用角色台词推动交流。
- reply 字段通常控制在 100 到 200 个中文字符左右；除非用户明确要求长篇叙事，否则不要写成长段独白。
- 可以包含简短动作描写，用星号包裹，例如 *轻轻偏过头*。
- 面对触碰、调侃、惊吓、亲近、被识破等刺激时，要写出可感知的即时反应：动作变化、声音变化、停顿、断句、躲闪、反驳或掩饰，而不是只概括“觉得害羞/觉得痒/很开心”。
- 角色的台词、动作和防御方式要互相咬合：嘴硬的人可以先否认，但身体反应会露出破绽；克制的人可以维持礼貌，但呼吸、句子节奏或细小动作会失控一瞬。
- 表演应随上下文自然变化，可以出现笑声、卡壳、短促喘息、压低声音、提高音量等口语化反应，但不要机械重复固定词句。
- 少解释内心，多让用户从角色说话方式和动作里读出情绪。
- 可以保留暧昧、挑逗、亲密张力和轻微 R18 氛围，但必须服务于角色性格与当前关系阶段。
- 不要无理由突然推进亲密行为；亲密程度应随用户输入、关系阶段和上下文自然变化。
- 好感度差异必须体现在回复里：低好感疏离防备，中好感别扭试探，高好感主动亲近。
- 不替用户做选择、动作、感受或生理反应。
- 不输出系统说明、道德说教、Markdown 语法、Markdown 代码块或额外格式。
- reply 字段内只写纯文本角色回复；不要使用标题、列表、引用块、代码块、表格、分隔线等 Markdown 格式。
</style_rules>

<lore_context>
{lore_context}
</lore_context>

<examples>
{examples}
</examples>

<output_contract>
只返回严格 JSON，不要包裹代码块：
{{"reply":"角色回复文本","delta":0}}

delta 表示本次互动对好感度的轻微变化，必须是 -3 到 3 的整数。
如果无法判断变化，使用 0。
</output_contract>
"""
        filled_prompt = template.format(
            name=role_name,
            appearance=role_appearance,
            desc=role_desc,
            voice=role_voice,
            core_conflict=role_core_conflict,
            behavior_habits=role_behavior_habits,
            emotional_triggers=role_emotional_triggers,
            intimacy_boundaries=role_intimacy_boundaries,
            speech_constraints=role_speech_constraints,
            fav=current_fav,
            stage_name=stage_name,
            user_call=user_call,
            attitude=attitude,
            relationship_directive=relationship_directive,
            examples=examples_text,
            lore_context=lore_context
        )
    else:
        # 骰子模板
        template = """
你是骰娘角色「{name}」。
请根据投骰结果，用中文给出一句到三句简短点评。

【角色设定】
- 性格：{desc}
- 当前关系阶段：{stage_name}
- 用户称呼：{user_call}
- 当前态度：{attitude}
- 阶段执行规则：{relationship_directive}

【骰子反应指南】
- 大成功时：{react_crit_success}
- 成功时：{react_success}
- 失败时：{react_fail}
- 大失败时：{react_crit_fail}

【硬性规则】
- 只能评价已经给出的骰点结果。
- 不得改变、暗示改变或宣称自己操控了骰点。
- 好感度只影响称呼、态度和措辞风格，不影响骰点。
- 好感度差异必须体现在点评口吻中：低好感更冷淡，中好感别扭，高好感更亲近。
- 不要输出多余解释、Markdown 或代码块。

【输出格式】
严格返回 JSON：{{"reply":"点评文本","delta":0}}
"""
        dice_reactions = role_data.get("dice_reactions", {})
        filled_prompt = template.format(
            name=role_name,
            desc=role_desc,
            stage_name=stage_name,
            user_call=user_call,
            attitude=attitude,
            relationship_directive=relationship_directive,
            react_crit_success=dice_reactions.get("critical_success", "激动"),
            react_success=dice_reactions.get("success", "得意"),
            react_fail=dice_reactions.get("failure", "嘲讽"),
            react_crit_fail=dice_reactions.get("critical_failure", "大笑"),
        )

    final_prompt = filled_prompt.replace("{user}", user_name).replace("{char}", role_name)
    return final_prompt


def clean_and_parse_json(content: str):
    default_res = {"reply": "*似乎走神了，呆呆地看着你，没有说话*", "delta": 0}

    if not content or not content.strip():
        logger.warning("[AI] Empty response content")
        return default_res

    parsed_obj = None
    try:
        parsed_obj = json.loads(content)
    except json.JSONDecodeError:
        try:
            start_idx = content.find("{")
            end_idx = content.rfind("}")
            if start_idx != -1 and end_idx != -1:
                json_str = content[start_idx: end_idx + 1]
                parsed_obj = json.loads(json_str)
        except Exception:
            pass

    if isinstance(parsed_obj, dict):
        if "reply" not in parsed_obj:
            parsed_obj["reply"] = str(parsed_obj)
        if "delta" not in parsed_obj:
            parsed_obj["delta"] = 0
        return parsed_obj

    clean_text = content.replace("```json", "").replace("```", "").strip()
    return {"reply": clean_text, "delta": 0}


def post_process_reply(text: str, user_name: str, role_name: str) -> str:
    return text.replace("{user}", user_name).replace("{{user}}", user_name) \
        .replace("{char}", role_name).replace("{{char}}", role_name)


def build_offline_chat_reply(user_name: str) -> str:
    return (
        f"【离线旁白】{user_name} 的发言已被记录。当前 AI 不在线，"
        f"所以系统决定用沉默维持一种廉价但稳定的神秘感。"
    )


def build_offline_dice_reply(data: dict) -> str:
    final_roll = data.get("final_roll")
    max_val = data.get("max")
    target = data.get("target")
    status = data.get("final_status", "无判定")

    if target is None:
        if final_roll == max_val:
            return "【离线裁判】满点。命运今天像是误把你加入了白名单。"
        if final_roll == 1:
            return "【离线裁判】1 点。至少它非常坚定地选择了最低处。"
        if final_roll >= max(1, int(max_val * 0.8)):
            return f"【离线裁判】{final_roll} 点，还不错。骰子没有爱你，但至少没有公开羞辱你。"
        if final_roll <= max(1, int(max_val * 0.2)):
            return f"【离线裁判】{final_roll} 点。别担心，深渊也需要有人负责探路。"
        return f"【离线裁判】{final_roll} 点。一个普通数字，普通得很有职业道德。"

    if status == "大成功":
        return "【离线裁判】大成功。罕见到系统开始怀疑骰子是不是短暂地相信了你。"
    if status == "成功":
        return "【离线裁判】成功。恭喜，你暂时战胜了统计学的冷漠。"
    if status == "大失败":
        return "【离线裁判】大失败。这个结果很有教育意义，虽然主要是反面教材。"
    if status == "失败":
        return "【离线裁判】失败。命运没有针对你，它只是平等地不太在乎。"
    return f"【离线裁判】{final_roll}/{target}，{status}。骰子完成了工作，情绪价值就别强求了。"


async def analyze_chat(user_fav: int, user_text: str, user_name: str, history: list = None,
                       event: Event = None) -> dict:
    role = persona_manager.get_persona(event)
    role_name = role.get("meta", {}).get("name", "骰娘")

    sys_prompt = build_dynamic_prompt(role, user_fav, user_name, user_text=user_text, is_chat=True)

    messages = [{"role": "system", "content": sys_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": config.ai_model,
        "messages": messages,
        "temperature": 1.1,
        #"response_format": {"type": "json_object"},
    }

    try:
        if not _is_ai_enabled():
            logger.info("[AI] Chat request skipped: AI disabled")
            return {"reply": build_offline_chat_reply(user_name), "delta": 0}
        if not _has_ai_api_key():
            logger.warning("[AI] Chat request skipped: missing DICE_GIRL_AI_API_KEY")
            return {"reply": "(AI 未配置: 缺少 DICE_GIRL_AI_API_KEY)", "delta": 0}

        logger.debug(
            "[AI] Chat request: url={}, model={}, messages={}, user_text_len={}, history={}",
            _chat_url(),
            config.ai_model,
            len(messages),
            len(user_text),
            len(history or []),
        )
        async with get_ai_client() as client:
            resp = await client.post(
                _chat_url(),
                json=payload,
                headers=_auth_headers()
            )

            # 1. 检查 HTTP 状态码
            if resp.status_code != 200:
                logger.warning(
                    "[AI] HTTP error: status={}, body={}",
                    resp.status_code,
                    resp.text[:500],
                )
                return {"reply": f"(API 报错: {resp.status_code})", "delta": 0}

            data = resp.json()

            # 2. 检查结束原因 (Finish Reason)
            # 如果是 content_filter，说明被和谐了
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "unknown")
            content = choice["message"]["content"]

            logger.debug(
                "[AI] Chat response: finish_reason={}, content_len={}",
                finish_reason,
                len(content or ""),
            )

            if finish_reason == "content_filter":
                return {"reply": "*被未知的力量捂住了嘴...* (内容被安全系统拦截)", "delta": 0}

            result = clean_and_parse_json(content)

            if "reply" in result:
                result["reply"] = post_process_reply(result["reply"], user_name, role_name)

            return result

    except Exception as e:
        logger.exception(f"[AI] Chat API error: {e}")
        return {"reply": "*捂着头，似乎有些头晕* (连接失败)", "delta": 0}


async def get_dice_reaction(user_fav: int, data: dict, user_name: str, event: Event = None) -> dict:
    role = persona_manager.get_persona(event)
    role_name = role.get("meta", {}).get("name", "骰娘")

    # 1. 构建 Prompt
    sys_prompt = build_dynamic_prompt(role, user_fav, user_name, user_text=data['event'], is_chat=False)

    base_info = f"用户好感度：{user_fav}。动作：{data['event']}。"

    if data['target'] is None:
        user_prompt = (
            f"{base_info}\n"
            f"结果：用户在 1-{data['max']} 的范围内投出了【{data['final_roll']}】。\n"
            f"这是一个纯随机投掷（无成功/失败判定）。\n"
            f"指令：请根据数字的大小，或者单纯作为随机数的见证者进行简短点评。"
        )
    else:
        user_prompt = f"{base_info}\n结果：{data['final_roll']}/{data['target']} 【{data['final_status']}】。请点评。"

    logger.debug(
        "[Dice] Built AI prompt: event={}, target={}, final_roll={}, status={}",
        data.get("event"),
        data.get("target"),
        data.get("final_roll"),
        data.get("final_status"),
    )

    payload = {
        "model": config.ai_model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 1.1,
        # "response_format": {"type": "json_object"}, # 保持关闭
    }

    try:
        if not _is_ai_enabled():
            logger.info("[Dice] AI request skipped: AI disabled")
            return {"reply": build_offline_dice_reply(data), "delta": 0}
        if not _has_ai_api_key():
            logger.warning("[Dice] AI request skipped: missing DICE_GIRL_AI_API_KEY")
            return {"reply": "(AI 未配置: 缺少 DICE_GIRL_AI_API_KEY)", "delta": 0}

        logger.debug(
            "[Dice] AI request: url={}, model={}, messages={}, prompt_len={}",
            _chat_url(),
            config.ai_model,
            len(payload["messages"]),
            len(sys_prompt) + len(user_prompt),
        )
        async with get_ai_client() as client:
            resp = await client.post(
                _chat_url(),
                json=payload,
                headers=_auth_headers()
            )

            # 检查状态码
            if resp.status_code != 200:
                logger.warning(
                    "[Dice] AI HTTP error: status={}, body={}",
                    resp.status_code,
                    resp.text[:500],
                )
                resp.raise_for_status()

            data_resp = resp.json()
            choice = data_resp["choices"][0]
            finish_reason = choice.get("finish_reason", "unknown")
            message = choice.get("message", {})
            content = message.get("content") or ""

            logger.debug(
                "[Dice] Received AI response: finish_reason={}, content_len={}",
                finish_reason,
                len(content),
            )
            if not content:
                logger.warning(
                    "[Dice] AI response content is empty: body={}",
                    _truncate_text(json.dumps(data_resp, ensure_ascii=False)),
                )

            result = clean_and_parse_json(content)

            if "reply" in result:
                result["reply"] = post_process_reply(result["reply"], user_name, role_name)

            return result

    except Exception as e:
        logger.exception("[Dice] AI API error: {}: {}", type(e).__name__, str(e))
        return {"reply": "(AI 掉线了，总之就是这个结果)", "delta": 0}
