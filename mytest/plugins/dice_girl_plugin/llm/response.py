import json

from nonebot import logger
from pydantic import ValidationError

from .schemas import validate_chat_reply, validate_dice_reaction


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
                json_str = content[start_idx : end_idx + 1]
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


def parse_chat_reply(content: str) -> dict:
    try:
        return validate_chat_reply(clean_and_parse_json(content))
    except ValidationError as e:
        logger.warning("[AI] Chat response schema validation failed: {}", e)
        fallback = clean_and_parse_json(content)
        reply_text = str(fallback.get("reply") or "").strip()
        if not reply_text:
            reply_text = "*似乎走神了，呆呆地看着你，没有说话*"
        return {"reply": reply_text, "delta": 0}


def parse_dice_reaction(content: str) -> dict:
    try:
        return validate_dice_reaction(clean_and_parse_json(content))
    except ValidationError as e:
        logger.warning("[Dice] Response schema validation failed: {}", e)
        fallback = clean_and_parse_json(content)
        reply_text = str(fallback.get("reply") or "").strip()
        if not reply_text:
            reply_text = "(AI 没有给出点评)"
        return {"reply": reply_text, "delta": 0}


def post_process_reply(text: str, user_name: str, role_name: str) -> str:
    return (
        text.replace("{user}", user_name)
        .replace("{{user}}", user_name)
        .replace("{char}", role_name)
        .replace("{{char}}", role_name)
    )


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
